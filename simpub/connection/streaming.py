"""
Implements the streaming connection for the hdar server

How does pub / sub work in zmq:
- https://learning-0mq-with-pyzmq.readthedocs.io/en/latest/pyzmq/patterns/pubsub.html
"""
from threading import Thread
from typing import Optional, Any
from simpub.loaders.json import JsonScene
import zmq
import zmq.decorators

class StreamReceiver:
  def __init__(self, context : zmq.Context, callback):
    self.zmq_context = context
    self._thread = Thread(target=self._loop)
    self.running : bool = False
    self.callback = callback
    self.conn = None

  def connect(self, addr : str, port : int):
    self.conn = addr, port
    self.running = True
    self._thread.start()

  def disconnect(self):
    self.running = False    
    self._thread.join()

  def _loop(self):
    sub_socket : zmq.Socket = self.zmq_context.socket(zmq.SUB)
    sub_socket.connect(f"tcp://{self.conn[0]}:{self.conn[1]}")
    try:
      while self.running:
        message = sub_socket.recv_string()
        if message is None: continue
        self.callback(message)
    finally:
      sub_socket.close(0)    
class StreamSender: 
  def __init__(self, context : zmq.Context, port : Optional[int] = None):
    self.zmq_context = context
    self.port = port
    self.pub_socket : zmq.Socket = self.zmq_context.socket(zmq.PUB)
    if self.port:
      self.pub_socket.bind(f"tcp://*:{self.port}")
    else: 
      self.port = self.pub_socket.bind_to_random_port(f"tcp://*")
    
  def stop(self):
    self.pub_socket.close(0)
  
  def publish(self, data : Any):
    self.pub_socket.send_string(JsonScene.to_string(data))
