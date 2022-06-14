"""Module for the receiver subprocess for end-to-end latency measurement."""

import itertools
from multiprocessing import connection
import os
import time
from typing import Any, Iterable, Optional

from absl import flags

from perfkitbenchmarker.scripts.messaging_service_scripts.common import client as common_client
from perfkitbenchmarker.scripts.messaging_service_scripts.common import errors
from perfkitbenchmarker.scripts.messaging_service_scripts.common.e2e import protocol
from perfkitbenchmarker.scripts.messaging_service_scripts.common.e2e import worker_utils

FLAGS = flags.FLAGS


def main(input_conn: connection.Connection,
         output_conn: connection.Connection,
         serialized_flags: str,
         app: Any,
         iterations: Optional[int] = None,
         pinned_cpus: Optional[Iterable[Any]] = None):
  """Runs the code for the receiver worker subprocess.

  Intended to be called with the multiprocessing.Process stdlib function.

  Args:
    input_conn: A connection object created with multiprocessing.Pipe to read
      data from the main process.
    output_conn: A connection object created with multiprocessing.Pipe to write
      data to the main process.
    serialized_flags: Flags from the main process serialized with
      flags.FLAGS.flags_into_string.
    app: Main process' app instance.
    iterations: Optional. The number of times the main loop will be run. If left
      unset, it will run forever (or until terminated by the main process).
    pinned_cpus: Optional. An iterable of CPU IDs to be passed to
      os.sched_setaffinity if set.
  """
  if pinned_cpus is not None:
    os.sched_setaffinity(0, pinned_cpus)
  FLAGS(serialized_flags.splitlines(), known_only=True)
  client = app.get_client_class().from_flags()
  try:
    communicator = worker_utils.Communicator(input_conn, output_conn)
    communicator.greet()
    times_iterable = itertools.repeat(0) if iterations is None else range(
        iterations)
    for _ in times_iterable:
      receive_cmd = communicator.await_from_main(
          protocol.Consume, protocol.AckConsume())
      deadline = time.time() + common_client.TIMEOUT
      try:
        reception_report = _receive_message(client, receive_cmd.seq, deadline)
        while not reception_report:
          reception_report = _receive_message(client, receive_cmd.seq, deadline)
      except Exception as e:  # pylint: disable=broad-except
        communicator.send(protocol.ReceptionReport(receive_error=repr(e)))
      else:
        communicator.send(reception_report)
  finally:
    client.close()


def _receive_message(
    client: common_client.BaseMessagingServiceClient,
    expected_seq: int,
    deadline: float) -> Optional[protocol.ReceptionReport]:
  """Consumes a message from client.

  Args:
    client: A BaseMessagingServiceClient instance the message is pulled from.
    expected_seq: The expected seq number of the message to receive.
    deadline: Maximum time (as returned by time.time()) to be pulling. It will
      be used to set the pulls' timeout.

  Returns:
    A ReceptionReport protocol object only if if the message has the expected
    seq number. Otherwise, None.

  Raises:
    A PullTimeoutOnReceiverError. If deadline time is reached.
  """
  timeout = deadline - time.time()
  if timeout < 0:
    raise errors.EndToEnd.PullTimeoutOnReceiverError
  message = client.pull_message(timeout)
  if message is None:  # i.e. on timeout
    return None
  pull_timestamp = time.time_ns()
  client.acknowledge_received_message(message)
  ack_timestamp = time.time_ns()
  seq = client.decode_seq_from_message(message)
  if seq != expected_seq:
    return None
  return protocol.ReceptionReport(
      seq=expected_seq,
      receive_timestamp=pull_timestamp,
      ack_timestamp=ack_timestamp,)
