#!/usr/bin/env python
"""Client actions related to administrating the client and its configuration."""

import logging
import os
import platform
import socket
import traceback

import cryptography
from cryptography.hazmat.backends import openssl
import pkg_resources
import psutil
import pytsk3
import yara

from grr_response_client import actions
from grr_response_client.client_actions import tempfiles
from grr_response_client.client_actions import timeline
from grr_response_client.unprivileged import sandbox
from grr_response_core import config
from grr_response_core.lib import config_lib
from grr_response_core.lib import rdfvalue
from grr_response_core.lib import utils
from grr_response_core.lib.rdfvalues import client as rdf_client
from grr_response_core.lib.rdfvalues import client_action as rdf_client_action
from grr_response_core.lib.rdfvalues import flows as rdf_flows
from grr_response_core.lib.rdfvalues import protodict as rdf_protodict


class Echo(actions.ActionPlugin):
  """Returns a message to the server."""

  in_rdfvalue = rdf_client_action.EchoRequest
  out_rdfvalues = [rdf_client_action.EchoRequest]

  def Run(self, args):
    self.SendReply(args)


def GetHostnameFromClient(args):
  del args  # Unused.
  yield rdf_protodict.DataBlob(string=socket.gethostname())


class GetHostname(actions.ActionPlugin):
  """Retrieves the host name of the client."""

  out_rdfvalues = [rdf_protodict.DataBlob]

  def Run(self, args):
    for res in GetHostnameFromClient(args):
      self.SendReply(res)


class GetPlatformInfo(actions.ActionPlugin):
  """Retrieves platform information."""

  out_rdfvalues = [rdf_client.Uname]

  def Run(self, unused_args):
    """Populate platform information into a Uname response."""
    self.SendReply(rdf_client.Uname.FromCurrentSystem())


class Kill(actions.ActionPlugin):
  """A client action for terminating (killing) the client.

  Used for testing process respawn.
  """

  out_rdfvalues = [rdf_flows.GrrMessage]

  def Run(self, unused_arg):
    """Run the kill."""
    # Send a message back to the service to say that we are about to shutdown.
    reply = rdf_flows.GrrStatus(status=rdf_flows.GrrStatus.ReturnedStatus.OK)
    # Queue up the response message, jump the queue.
    self.SendReply(reply, message_type=rdf_flows.GrrMessage.Type.STATUS)

    # Give the http thread some time to send the reply.
    self.grr_worker.Sleep(10)

    # Die ourselves.
    logging.info("Dying on request.")
    os._exit(242)  # pylint: disable=protected-access


class GetConfiguration(actions.ActionPlugin):
  """Retrieves the running configuration parameters."""

  in_rdfvalue = None
  out_rdfvalues = [rdf_protodict.Dict]

  def Run(self, unused_arg):
    """Retrieve the configuration except for the blocked parameters."""

    out = self.out_rdfvalues[0]()
    for descriptor in config.CONFIG.type_infos:
      try:
        value = config.CONFIG.Get(descriptor.name, default=None)
      except (config_lib.Error, KeyError, AttributeError, ValueError) as e:
        logging.info("Config reading error: %s", e)
        continue

      if value is not None:
        out[descriptor.name] = value

    self.SendReply(out)


class GetLibraryVersions(actions.ActionPlugin):
  """Retrieves version information for installed libraries."""

  in_rdfvalue = None
  out_rdfvalues = [rdf_protodict.Dict]

  def GetSSLVersion(self):
    return openssl.backend.openssl_version_text()

  def GetCryptographyVersion(self):
    return cryptography.__version__

  def GetPSUtilVersion(self):
    return ".".join(map(utils.SmartUnicode, psutil.version_info))

  def GetProtoVersion(self):
    return pkg_resources.get_distribution("protobuf").version

  def GetTSKVersion(self):
    return pytsk3.TSK_VERSION_STR

  def GetPyTSKVersion(self):
    return pytsk3.get_version()

  def GetYaraVersion(self):
    return yara.YARA_VERSION

  library_map = {
      "pytsk": GetPyTSKVersion,
      "TSK": GetTSKVersion,
      "cryptography": GetCryptographyVersion,
      "SSL": GetSSLVersion,
      "psutil": GetPSUtilVersion,
      "yara": GetYaraVersion,
  }

  error_str = "Unable to determine library version: %s"

  def Run(self, unused_arg):
    result = self.out_rdfvalues[0]()
    for lib, f in self.library_map.items():
      try:
        result[lib] = f(self)
      except Exception:  # pylint: disable=broad-except
        result[lib] = self.error_str % traceback.format_exc()

    self.SendReply(result)


class UpdateConfiguration(actions.ActionPlugin):
  """Updates configuration parameters on the client."""

  in_rdfvalue = rdf_protodict.Dict

  UPDATABLE_FIELDS = {"Client.foreman_check_frequency",
                      "Client.server_urls",
                      "Client.max_post_size",
                      "Client.max_out_queue",
                      "Client.poll_min",
                      "Client.poll_max",
                      "Client.rss_max"}  # pyformat: disable

  def _UpdateConfig(self, filtered_arg, config_obj):
    for field, value in filtered_arg.items():
      config_obj.Set(field, value)

    try:
      config_obj.Write()
    except (IOError, OSError):
      pass

  def Run(self, arg):
    """Does the actual work."""
    try:
      if self.grr_worker.client.FleetspeakEnabled():
        raise ValueError("Not supported on Fleetspeak enabled clients.")
    except AttributeError:
      pass

    smart_arg = {str(field): value for field, value in arg.items()}

    disallowed_fields = [
        field
        for field in smart_arg
        if field not in UpdateConfiguration.UPDATABLE_FIELDS
    ]

    if disallowed_fields:
      raise ValueError(
          "Received an update request for restricted field(s) %s."
          % ",".join(disallowed_fields)
      )

    if platform.system() != "Windows":
      # Check config validity before really applying the changes. This isn't
      # implemented for our Windows clients though, whose configs are stored in
      # the registry, as opposed to in the filesystem.

      canary_config = config.CONFIG.CopyConfig()

      # Prepare a temporary file we'll write changes to.
      with tempfiles.CreateGRRTempFile(mode="w+") as temp_fd:
        temp_filename = temp_fd.name

      # Write canary_config changes to temp_filename.
      canary_config.SetWriteBack(temp_filename)
      self._UpdateConfig(smart_arg, canary_config)

      try:
        # Assert temp_filename is usable by loading it.
        canary_config.SetWriteBack(temp_filename)
      # Wide exception handling passed here from config_lib.py...
      except Exception:  # pylint: disable=broad-except
        logging.warning("Updated config file %s is not usable.", temp_filename)
        raise

      # If temp_filename works, remove it (if not, it's useful for debugging).
      os.unlink(temp_filename)

    # The changes seem to work, so push them to the real config.
    self._UpdateConfig(smart_arg, config.CONFIG)


def GetClientInformation() -> rdf_client.ClientInformation:
  return rdf_client.ClientInformation(
      client_name=config.CONFIG["Client.name"],
      client_binary_name=psutil.Process().name(),
      client_description=config.CONFIG["Client.description"],
      client_version=int(config.CONFIG["Source.version_numeric"]),
      build_time=config.CONFIG["Client.build_time"],
      labels=config.CONFIG.Get("Client.labels", default=None),
      timeline_btime_support=timeline.BTIME_SUPPORT,
      sandbox_support=sandbox.IsSandboxInitialized(),
  )


class GetClientInfo(actions.ActionPlugin):
  """Obtains information about the GRR client installed."""

  out_rdfvalues = [rdf_client.ClientInformation]

  def Run(self, unused_args):
    self.SendReply(GetClientInformation())


class SendStartupInfo(actions.ActionPlugin):

  in_rdfvalue = None
  out_rdfvalues = [rdf_client.StartupInfo]

  well_known_session_id = rdfvalue.SessionID(flow_name="Startup")

  def _CheckInterrogateTrigger(self) -> bool:
    interrogate_trigger_path = config.CONFIG["Client.interrogate_trigger_path"]
    if not interrogate_trigger_path:
      logging.info(
          "Client.interrogate_trigger_path not set, skipping the check."
      )
      return False

    if not os.path.exists(interrogate_trigger_path):
      logging.info(
          "Interrogate trigger file (%s) does not exist.",
          interrogate_trigger_path,
      )
      return False

    logging.info(
        "Interrogate trigger file exists: %s", interrogate_trigger_path
    )

    # First try to remove the file and return True only if the removal
    # is successful. This is to prevent a permission error + a crash loop from
    # trigger infinite amount of interrogations.
    try:
      os.remove(interrogate_trigger_path)
    except (OSError, IOError) as e:
      logging.exception(
          "Not triggering interrogate - failed to remove the "
          "interrogate trigger file (%s): %s",
          interrogate_trigger_path,
          e,
      )
      return False

    return True

  def Run(self, unused_arg, ttl=None):
    """Returns the startup information."""
    logging.debug("Sending startup information.")

    boot_time = rdfvalue.RDFDatetime.FromSecondsSinceEpoch(psutil.boot_time())
    response = rdf_client.StartupInfo(
        boot_time=boot_time,
        client_info=GetClientInformation(),
        interrogate_requested=self._CheckInterrogateTrigger(),
    )

    self.grr_worker.SendReply(
        response,
        session_id=self.well_known_session_id,
        response_id=0,
        request_id=0,
        message_type=rdf_flows.GrrMessage.Type.MESSAGE,
        ttl=ttl,
    )
