import io
from hashlib import md5
import random
import math

import numpy as np
import trimesh

from simpub.core.simpub_server import SimPublisher
from simpub.simdata import (
    SimScene,
    SimObject,
    SimVisual,
    VisualType,
    SimTransform,
    SimMesh,
)

import omni
import omni.usd
from pxr import Usd, UsdGeom, Gf, UsdUtils

from usdrt import Usd as RtUsd
from usdrt import UsdGeom as RtGeom
from usdrt import Rt


#! todo: separate the parser and the publisher...
class IsaacSimPublisher(SimPublisher):
    def __init__(self, host: str, stage: Usd.Stage) -> None:
        # self.sim_scene = None
        # self.rt_stage = None
        self.tracked_prims: list[dict] = []

        self.parse_scene(stage)
        super().__init__(self.sim_scene, host)

    def parse_scene(self, stage: Usd.Stage) -> SimScene:
        print("parsing stage:", stage)

        #! todo: maybe only do this when using gpu
        # usdrt is the api to read from fabric directly
        # check https://docs.omniverse.nvidia.com/kit/docs/usdrt/latest/docs/usd_fabric_usdrt.html
        self.use_usdrt_stage(stage)

        scene = SimScene()
        scene.root = SimObject(name="root", trans=SimTransform())
        self.sim_scene = scene

        # parse usd stage
        root_path = "/World"
        usd_obj = self.parse_prim_tree(root=stage.GetPrimAtPath(root_path))
        assert usd_obj is not None
        scene.root.children.append(usd_obj)

    def use_usdrt_stage(self, stage: Usd.Stage):
        """
        create a usdrt stage from a plain usd stage.
        only usdrt api can access updated object states from fabric.
        """
        stage_id = UsdUtils.StageCache.Get().Insert(stage)
        stage_id = stage_id.ToLongInt()
        print("usd stage id:", stage_id)

        rtstage = RtUsd.Stage.Attach(stage_id)
        print("usdrt stage:", rtstage)

        self.rt_stage = rtstage

    def parse_prim_tree(
        self,
        root: Usd.Prim,
        indent=0,
        parent_path=None,
    ) -> SimObject | None:
        """parse the tree starting from a prim"""

        # define prim types that are not ignored
        if root.GetTypeName() not in {
            "",
            "Xform",
            "Mesh",
            "Scope",
            "Cube",
            "Capsule",
            "Cone",
            "Cylinder",
            "Sphere",
        }:
            #! todo: perhaps traverse twice and preserve only prims with meshes as children
            return

        # filter out colliders prims
        purpose_attr = root.GetAttribute("purpose")
        if purpose_attr and purpose_attr.Get() in {"proxy", "guide"}:
            return

        # compute usd path of current prim
        if parent_path is None:
            prim_path = str(root.GetPrimPath())
        else:
            prim_path = f"{parent_path}/{root.GetName()}"

        # compute local transforms
        translate, rot, scale = self.compute_local_trans(root)

        # create a node for current prim
        sim_object = SimObject(
            name=prim_path.replace("/", "_"),
            trans=SimTransform(pos=translate, rot=rot, scale=scale),
        )

        print(
            "\t" * indent
            + f"{prim_path}: {root.GetTypeName()} {root.GetAttribute('purpose').Get()} {sim_object.trans.scale}"
        )

        # parse meshes and other primitive shapes
        self.parse_prim_geometries(
            prim=root,
            prim_path=prim_path,
            sim_obj=sim_object,
            indent=indent,
        )

        # track prims with rigid objects attached
        if (attr := root.GetAttribute("physics:rigidBodyEnabled")) and attr.Get():
            print("\t" * indent + f"tracking {prim_path}")
            self.tracked_prims.append(
                {"name": sim_object.name, "prim": root, "prim_path": prim_path}
            )

        child: Usd.Prim
        if root.IsInstance():
            # handle the case where root is an instance of a prototype
            proto = root.GetPrototype()
            print("\t" * indent + f"@prototype: {proto.GetPrimPath()}")

            # parse child prims of the prototype
            for child in proto.GetChildren():
                if obj := self.parse_prim_tree(
                    root=child, indent=indent + 1, parent_path=prim_path
                ):
                    sim_object.children.append(obj)

        else:
            # parse child prims of the current prim (root)
            for child in root.GetChildren():
                if obj := self.parse_prim_tree(
                    root=child, indent=indent + 1, parent_path=prim_path
                ):
                    sim_object.children.append(obj)

        return sim_object

    def compute_local_trans(self, prim: Usd.Prim):
        # not really necessary...
        timeline = omni.timeline.get_timeline_interface()
        timecode = timeline.get_current_time() * timeline.get_time_codes_per_seconds()

        # extract local transformation
        trans_mat = omni.usd.get_local_transform_matrix(prim, timecode)

        # get scale
        x_scale = Gf.Vec3d(
            trans_mat[0][0], trans_mat[0][1], trans_mat[0][2]
        ).GetLength()
        y_scale = Gf.Vec3d(
            trans_mat[1][0], trans_mat[1][1], trans_mat[1][2]
        ).GetLength()
        z_scale = Gf.Vec3d(
            trans_mat[2][0], trans_mat[2][1], trans_mat[2][2]
        ).GetLength()
        scale = [x_scale, z_scale, y_scale]

        # get translation
        translate = trans_mat.ExtractTranslation()
        translate = [-translate[1], translate[2], translate[0]]

        # get rotation
        rot = trans_mat.ExtractRotationQuat()
        imag = rot.GetImaginary()
        rot = [imag[1], -imag[2], -imag[0], rot.GetReal()]

        return translate, rot, scale

    def parse_prim_geometries(
        self,
        prim: Usd.Prim,
        prim_path: str,
        sim_obj: SimObject,
        indent: int,
    ):
        prim_type = prim.GetTypeName()

        if prim_type == "Mesh":
            # currently each instance of a prototype will create a different mesh object
            # detecting this and use the same mesh object would reduce memory usage

            # for soft body, maybe use usdrt.UsdGeom.xxx (in get_update() function, not here)
            mesh_prim = UsdGeom.Mesh(prim)
            assert mesh_prim is not None

            vertices = np.asarray(mesh_prim.GetPointsAttr().Get(), dtype=np.float32)
            normals = np.asarray(mesh_prim.GetNormalsAttr().Get(), dtype=np.float32)
            indices = np.asarray(
                mesh_prim.GetFaceVertexIndicesAttr().Get(), dtype=np.int32
            )
            face_vertex_counts = np.asarray(
                mesh_prim.GetFaceVertexCountsAttr().Get(), dtype=np.int32
            )

            # assuming there are either only triangular faces or only quad faces...
            assert len(set(face_vertex_counts)) == 1
            num_vert_per_face = face_vertex_counts[0]

            mesh_obj = trimesh.Trimesh(
                vertices=vertices, faces=indices.reshape(-1, num_vert_per_face)
            )

            # validate mesh data... (not really necessary)
            vertices = vertices.flatten()
            normals = normals.flatten()
            indices = indices.flatten()
            print(
                "\t" * indent
                + f"vertices size: {vertices.shape[0] // 3} {vertices.shape[0] % 3}"
            )
            print(
                "\t" * indent
                + f"normals size: {normals.shape[0] // 3} {normals.shape[0] % 3}"
            )
            print(
                "\t" * indent
                + f"triangles: {indices.shape[0] // 3} {indices.shape[0] % 3}"
            )
            assert normals.shape[0] // 3 == indices.shape[0]
            print(
                "\t" * indent
                + f"normal per index: {normals.shape[0] // 3} {indices.shape[0]}"
            )

            sim_mesh = self.build_mesh_buffer(mesh_obj,prim_path)
            sim_obj.visuals.append(sim_mesh)

        elif prim_type == "Cube":
            rt_prim = self.rt_stage.GetPrimAtPath(prim_path)
            # only handle scale...
            # translation: xformOp:translate
            # rotation: xformOp:orient
            cube_scale = rt_prim.GetAttribute("xformOp:scale").Get()

            cube_prim = RtGeom.Cube(rt_prim)
            cube_size = cube_prim.GetSizeAttr().Get()

            sim_cube = SimVisual(
                type=VisualType.CUBE,
                trans=SimTransform(
                    scale=[
                        cube_size * cube_scale[1],
                        cube_size * cube_scale[2],
                        cube_size * cube_scale[0],
                    ]
                ),
                color=[1.0, 1.0, 1.0, 1.0],
            )

            sim_obj.visuals.append(sim_cube)
            sim_obj.trans.scale = [1.0] * 3

        elif prim_type == "Capsule":
            rt_prim = self.rt_stage.GetPrimAtPath(prim_path)
            cap_prim = RtGeom.Capsule(rt_prim)

            axis = cap_prim.GetAxisAttr().Get()
            height = cap_prim.GetHeightAttr().Get()
            radius = cap_prim.GetRadiusAttr().Get()

            capsule_mesh = trimesh.creation.capsule(height=height, radius=radius)
            if axis == "Y":
                capsule_mesh.apply_transform(
                    trimesh.transformations.rotation_matrix(-math.pi / 2, [1, 0, 0])
                )
            elif axis == "X":
                capsule_mesh.apply_transform(
                    trimesh.transformations.rotation_matrix(math.pi / 2, [0, 1, 0])
                )

            # scale/translation/rotation not handled,
            # since it seems that isaac lab won't modify them...

            sim_mesh = self.build_mesh_buffer(capsule_mesh,prim_path)
            sim_obj.visuals.append(sim_mesh)

        elif prim_type == "Cone":
            rt_prim = self.rt_stage.GetPrimAtPath(prim_path)
            cap_prim = RtGeom.Cone(rt_prim)

            axis = cap_prim.GetAxisAttr().Get()
            height = cap_prim.GetHeightAttr().Get()
            radius = cap_prim.GetRadiusAttr().Get()

            cone_mesh = trimesh.creation.cone(height=height, radius=radius)
            cone_mesh.apply_transform(
                trimesh.transformations.translation_matrix([0, 0, -height * 0.5])
            )
            if axis == "Y":
                cone_mesh.apply_transform(
                    trimesh.transformations.rotation_matrix(-math.pi / 2, [1, 0, 0])
                )
            elif axis == "X":
                cone_mesh.apply_transform(
                    trimesh.transformations.rotation_matrix(math.pi / 2, [0, 1, 0])
                )

            # scale/translation/rotation not handled,
            # since it seems that isaac lab won't modify them...

            sim_mesh = self.build_mesh_buffer(cone_mesh,prim_path)
            sim_obj.visuals.append(sim_mesh)

        elif prim_type == "Cylinder":
            rt_prim = self.rt_stage.GetPrimAtPath(prim_path)
            cap_prim = RtGeom.Cylinder(rt_prim)

            axis = cap_prim.GetAxisAttr().Get()
            height = cap_prim.GetHeightAttr().Get()
            radius = cap_prim.GetRadiusAttr().Get()

            cylinder_mesh = trimesh.creation.cylinder(height=height, radius=radius)
            if axis == "Y":
                cylinder_mesh.apply_transform(
                    trimesh.transformations.rotation_matrix(-math.pi / 2, [1, 0, 0])
                )
            elif axis == "X":
                cylinder_mesh.apply_transform(
                    trimesh.transformations.rotation_matrix(math.pi / 2, [0, 1, 0])
                )

            # scale/translation/rotation not handled,
            # since it seems that isaac lab won't modify them...

            sim_mesh = self.build_mesh_buffer(cylinder_mesh,prim_path)
            sim_obj.visuals.append(sim_mesh)

        elif prim_type == "Sphere":
            rt_prim = self.rt_stage.GetPrimAtPath(prim_path)
            cap_prim = RtGeom.Sphere(rt_prim)

            radius = cap_prim.GetRadiusAttr().Get()

            sphere_mesh = trimesh.creation.uv_sphere(radius=radius)

            # scale/translation/rotation not handled,
            # since it seems that isaac lab won't modify them...

            sim_mesh = self.build_mesh_buffer(sphere_mesh,prim_path)
            sim_obj.visuals.append(sim_mesh)

    def build_mesh_buffer(self, mesh_obj: trimesh.Trimesh,mesh_name=None):
        mesh_obj.apply_transform(
            trimesh.transformations.euler_matrix(-math.pi / 2.0, math.pi / 2.0, 0)
        )

        # this will create smooth vertex normals.
        # in isaac sim the same vertex on different faces can have different normals,
        # but this is not supported by simpub, so here per-vertex normals are calculated.
        mesh_obj.fix_normals()

        # fill some buffers
        bin_buffer = io.BytesIO()

        # Vertices
        verts = mesh_obj.vertices.astype(np.float32)
        verts[:, 2] = -verts[:, 2]
        verts = verts.flatten()
        vertices_layout = bin_buffer.tell(), verts.shape[0]
        bin_buffer.write(verts)

        # Normals
        norms = mesh_obj.vertex_normals.astype(np.float32)
        norms[:, 2] = -norms[:, 2]
        norms = norms.flatten()
        normal_layout = bin_buffer.tell(), norms.shape[0]
        bin_buffer.write(norms)

        # Indices
        indices = mesh_obj.faces.astype(np.int32)
        indices = indices[:, [2, 1, 0]]
        indices = indices.flatten()
        indices_layout = bin_buffer.tell(), indices.shape[0]
        bin_buffer.write(indices)

        # Texture coords
        uv_layout = (0, 0)
        if hasattr(mesh_obj.visual, "uv"):
            uvs = mesh_obj.visual.uv.astype(np.float32)
            uvs[:, 1] = 1 - uvs[:, 1]
            uvs = uvs.flatten()
            uv_layout = bin_buffer.tell(), uvs.shape[0]

        bin_data = bin_buffer.getvalue()
        hash = md5(bin_data).hexdigest()

        #! todo: do not create new mesh when multiple primitives point to the same prototype
        # mesh_id = "@mesh-" + str(random.randint(int(1e9), int(1e10 - 1)))
        if (mesh_name is None):
            mesh_name="@mesh-" + str(random.randint(int(1e9), int(1e10 - 1)))
        mesh = SimMesh(
            name=mesh_name,
            indicesLayout=indices_layout,
            verticesLayout=vertices_layout,
            normalsLayout=normal_layout,
            uvLayout=uv_layout,
            dataHash=hash,
        )

        assert self.sim_scene is not None
        self.sim_scene.meshes.append(mesh)
        self.sim_scene.raw_data[mesh.dataHash] = bin_data

        sim_mesh = SimVisual(
            type=VisualType.MESH,
            mesh=mesh_name,
            color=[1.0, 1.0, 1.0, 1.0],
            trans=SimTransform(),
        )
        return sim_mesh

    def get_update(self) -> dict[str, list[float]]:
        import omni
        import omni.usd

        from pxr import Usd, UsdUtils
        from usdrt import Usd as RtUsd
        from usdrt import UsdGeom as RtGeom
        from usdrt import Rt

        def print_state():
            prim = self.rt_stage.GetPrimAtPath("/World/Origin1/Robot/panda_hand")
            print(prim)
            print(prim.GetTypeName())

            prim = Rt.Xformable(prim)
            print(prim.GetWorldPositionAttr().Get())

            rot = prim.GetWorldOrientationAttr().Get()
            print(rot)
            print(
                rot.GetReal(),
                rot.GetImaginary()[0],
                rot.GetImaginary()[1],
                rot.GetImaginary()[2],
            )

        # print_state()

        state = {}

        # for name, trans in self.tracked_obj_trans.items():
        #     pos, rot = trans
        #     state[name] = [-pos[1], pos[2], pos[0], rot[2], -rot[3], -rot[1], rot[0]]

        timeline = omni.timeline.get_timeline_interface()
        timecode = timeline.get_current_time() * timeline.get_time_codes_per_seconds()

        # print()
        # print(timecode)
        for tracked_prim in self.tracked_prims:
            prim_name = tracked_prim["name"]
            prim_path = tracked_prim["prim_path"]
            prim = tracked_prim["prim"]

            # cur_trans = get_physx_interface().get_rigidbody_transformation(prim_path)
            # print(cur_trans)

            trans_mat = omni.usd.get_world_transform_matrix(prim, timecode)
            # print(f"{prim_name}: {trans_mat}")

            translate = trans_mat.ExtractTranslation()
            translate = [-translate[1], translate[2], translate[0]]

            rot = trans_mat.ExtractRotationQuat()
            imag = rot.GetImaginary()
            rot = [imag[1], -imag[2], -imag[0], rot.GetReal()]

            state[prim_name] = [
                translate[0],
                translate[1],
                translate[2],
                rot[0],
                rot[1],
                rot[2],
                rot[3],
            ]

            rt_prim = self.rt_stage.GetPrimAtPath(prim_path)
            # print(rt_prim)
            # print(rt_prim.GetTypeName())

            rt_prim = Rt.Xformable(rt_prim)
            pos = rt_prim.GetWorldPositionAttr().Get()
            rot = rt_prim.GetWorldOrientationAttr().Get()

            # print(rt_prim.GetWorldPositionAttr().Get())
            # print(rot)
            # print(
            #     rot.GetReal(),
            #     rot.GetImaginary()[0],
            #     rot.GetImaginary()[1],
            #     rot.GetImaginary()[2],
            # )

            state[prim_name] = [
                -pos[1],
                pos[2],
                pos[0],
                rot.GetImaginary()[1],
                -rot.GetImaginary()[2],
                -rot.GetImaginary()[0],
                rot.GetReal(),
            ]

        return state
