#!/usr/bin/env python
"""Tests for API client and approvals-related API calls."""

import threading
import time
from unittest import mock

from absl import app

from grr_response_core import config
from grr_response_server.gui import api_auth_manager
from grr_response_server.gui import api_call_router_with_approval_checks
from grr_response_server.gui import api_integration_test_lib
from grr.test_lib import hunt_test_lib
from grr.test_lib import test_lib


class ApiClientLibApprovalsTest(
    api_integration_test_lib.ApiIntegrationTest,
    hunt_test_lib.StandardHuntTestMixin,
):

  def setUp(self):
    super().setUp()

    cls = api_call_router_with_approval_checks.ApiCallRouterWithApprovalChecks
    cls.ClearCache()

    config_overrider = test_lib.ConfigOverrider(
        {"API.DefaultRouter": cls.__name__}
    )
    config_overrider.Start()
    self.addCleanup(config_overrider.Stop)

    # Force creation of new APIAuthorizationManager, so that configuration
    # changes are picked up.
    api_auth_manager.InitializeApiAuthManager()

  def testCreateClientApproval(self):
    with mock.patch.object(time, "time") as mock_now:
      oneday_s = 24 * 60 * 60
      mock_now.return_value = oneday_s  # 'Now' is 1 day past epoch

      # 'Now' is one day past epoch, plus default expiration duration
      twentyninedays_us = (
          config.CONFIG["ACL.token_expiry"] * 1000000
      ) + oneday_s * 1e6

      client_id = self.SetupClient(0)
      self.CreateUser("foo")

      approval = self.api.Client(client_id).CreateApproval(
          reason="blah",
          notified_users=["foo"],
          email_cc_addresses=["bar@some.example"],
      )
      self.assertEqual(approval.client_id, client_id)
      self.assertEqual(approval.data.subject.client_id, client_id)
      self.assertEqual(approval.data.reason, "blah")
      self.assertFalse(approval.data.is_valid)
      self.assertEqual(approval.data.email_cc_addresses, ["bar@some.example"])
      self.assertEqual(approval.data.expiration_time_us, twentyninedays_us)

  def testCreateClientApprovalNonDefaultExpiration(self):
    """Tests requesting approval with a non-default expiration duration."""
    with mock.patch.object(time, "time") as mock_now:
      mock_now.return_value = 24 * 60 * 60  # 'time.time' is 1 day past epoch

      onetwentydays = 120
      onetwentyonedays_us = 121 * 24 * 60 * 60 * 1000000

      client_id = self.SetupClient(0)
      self.CreateUser("foo")

      approval = self.api.Client(client_id).CreateApproval(
          reason="blah",
          notified_users=["foo"],
          expiration_duration_days=onetwentydays,
      )
      self.assertEqual(approval.client_id, client_id)
      self.assertEqual(approval.data.subject.client_id, client_id)
      self.assertEqual(approval.data.reason, "blah")
      self.assertFalse(approval.data.is_valid)
      self.assertEqual(approval.data.email_cc_addresses, [])
      self.assertEqual(approval.data.expiration_time_us, onetwentyonedays_us)

  def testWaitUntilClientApprovalValid(self):
    client_id = self.SetupClient(0)
    self.CreateUser("foo")

    approval = self.api.Client(client_id).CreateApproval(
        reason="blah", notified_users=["foo"]
    )
    self.assertFalse(approval.data.is_valid)

    def ProcessApproval():
      time.sleep(1)
      self.GrantClientApproval(
          client_id,
          requestor=self.test_username,
          approval_id=approval.approval_id,
          approver="foo",
      )

    thread = threading.Thread(name="ProcessApprover", target=ProcessApproval)
    thread.start()
    try:
      result_approval = approval.WaitUntilValid()
      self.assertTrue(result_approval.data.is_valid)
    finally:
      thread.join()

  def testCreateHuntApproval(self):
    h_id = self.StartHunt()
    self.CreateUser("foo")

    approval = self.api.Hunt(h_id).CreateApproval(
        reason="blah", notified_users=["foo"]
    )
    self.assertEqual(approval.hunt_id, h_id)
    self.assertEqual(approval.data.subject.hunt_id, h_id)
    self.assertEqual(approval.data.reason, "blah")
    self.assertFalse(approval.data.is_valid)

  def testWaitUntilHuntApprovalValid(self):
    self.CreateUser("approver")
    h_id = self.StartHunt()

    approval = self.api.Hunt(h_id).CreateApproval(
        reason="blah", notified_users=["approver"]
    )
    self.assertFalse(approval.data.is_valid)

    def ProcessApproval():
      time.sleep(1)
      self.GrantHuntApproval(
          h_id,
          requestor=self.test_username,
          approval_id=approval.approval_id,
          approver="approver",
      )

    ProcessApproval()
    thread = threading.Thread(name="HuntApprover", target=ProcessApproval)
    thread.start()
    try:
      result_approval = approval.WaitUntilValid()
      self.assertTrue(result_approval.data.is_valid)
    finally:
      thread.join()


def main(argv):
  test_lib.main(argv)


if __name__ == "__main__":
  app.run(main)
