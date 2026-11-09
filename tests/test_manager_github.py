import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch
from urllib.parse import unquote, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "sources"))
with patch.dict(os.environ, {"INPUT_GH_TOKEN": "test-api-token", "MOCK_WAKATIME": "true"}):
    import manager_github


class GitHubAuthenticationTests(unittest.TestCase):
    def setUp(self):
        environment = patch.multiple(
            manager_github.EM,
            GH_TOKEN="test-api-token",
            PUSH_TOKEN="",
            GH_USER="",
            DEBUG_RUN=False,
            COMMIT_SINGLE=False,
            PULL_BRANCH_NAME="",
            PUSH_BRANCH_NAME="",
        )
        environment.start()
        self.addCleanup(environment.stop)
        self.github = MagicMock()
        self.github.get_user.return_value = SimpleNamespace(login="example-user")
        self.github.get_repo.return_value = SimpleNamespace(default_branch="main")
        self.push_github = MagicMock()
        self.push_github.get_repo.return_value = SimpleNamespace(permissions=SimpleNamespace(push=True))
        self.client = patch.object(manager_github, "Github", side_effect=[self.github, self.push_github]).start()
        self.clone = patch.object(manager_github.Repo, "clone_from").start()
        self.remove = patch.object(manager_github, "rmtree").start()
        patch.object(manager_github.DBM, "i").start()
        self.addCleanup(patch.stopall)

    def assert_credentials(self, expected_token):
        url = self.clone.call_args.args[0]
        parsed = urlsplit(url)
        self.assertEqual(parsed.username, "x-access-token")
        self.assertEqual(unquote(parsed.password or ""), expected_token)
        self.assertEqual(parsed.hostname, "github.com")
        self.assertEqual(parsed.path, "/example-user/example-user.git")
        self.assertEqual(self.clone.call_args.kwargs, {"to_path": "repo"})
        self.clone.return_value.git.checkout.assert_called_once_with("main")

    def test_gh_token_used_when_push_token_missing(self):
        manager_github.GitHubManager.prepare_github_env()
        self.assert_credentials("test-api-token")
        self.assertEqual(self.client.call_count, 1)

    def test_separate_push_token_with_write_permission(self):
        with patch.object(manager_github.EM, "PUSH_TOKEN", "test-workflow-token"):
            manager_github.GitHubManager.prepare_github_env()
        self.assert_credentials("test-workflow-token")
        self.push_github.get_repo.assert_called_once_with("example-user/example-user")

    def test_denied_push_token_falls_back_to_gh_token(self):
        self.push_github.get_repo.return_value.permissions.push = False
        with patch.object(manager_github.EM, "PUSH_TOKEN", "test-readonly-token"):
            manager_github.GitHubManager.prepare_github_env()
        self.assert_credentials("test-api-token")

    def test_missing_permissions_fall_back_to_gh_token(self):
        self.push_github.get_repo.return_value.permissions = None
        with patch.object(manager_github.EM, "PUSH_TOKEN", "test-workflow-token"):
            manager_github.GitHubManager.prepare_github_env()
        self.assert_credentials("test-api-token")

    def test_permission_api_failure_falls_back_to_gh_token(self):
        self.push_github.get_repo.side_effect = RuntimeError("Offline permission check failure")
        with patch.object(manager_github.EM, "PUSH_TOKEN", "test-workflow-token"):
            manager_github.GitHubManager.prepare_github_env()
        self.assert_credentials("test-api-token")

    def test_identical_tokens_skip_permission_check(self):
        with patch.object(manager_github.EM, "PUSH_TOKEN", "test-api-token"):
            manager_github.GitHubManager.prepare_github_env()
        self.assert_credentials("test-api-token")
        self.assertEqual(self.client.call_count, 1)

    def test_token_is_url_encoded(self):
        token = "test-token:@/ +%?&#"
        with patch.object(manager_github.EM, "PUSH_TOKEN", token):
            manager_github.GitHubManager.prepare_github_env()
        self.assert_credentials(token)
        self.assertNotIn(token, self.clone.call_args.args[0])

    def test_debug_run_does_not_clone_or_remove_files(self):
        with patch.object(manager_github.EM, "DEBUG_RUN", True):
            manager_github.GitHubManager.prepare_github_env()
        self.clone.assert_not_called()
        self.remove.assert_not_called()
        self.assertIsNone(manager_github.GitHubManager.REPO)

    def test_git_obtains_complete_credentials_without_prompting(self):
        with patch.object(manager_github.EM, "PUSH_TOKEN", "test-workflow-token"):
            manager_github.GitHubManager.prepare_github_env()
        url = self.clone.call_args.args[0]
        env = dict(
            os.environ,
            GIT_CONFIG_NOSYSTEM="1",
            GIT_CONFIG_GLOBAL=os.devnull,
            GIT_CONFIG_COUNT="0",
            GIT_TERMINAL_PROMPT="0",
            GIT_ASKPASS="",
            SSH_ASKPASS="",
            GCM_INTERACTIVE="Never",
        )
        # Git's credential plumbing is local only: no server, real secrets, or user credential helper is used.
        with TemporaryDirectory() as directory:
            result = subprocess.run(
                ["git", "-c", "credential.helper=", "credential", "fill"],
                input=f"url={url}\n\n",
                text=True,
                capture_output=True,
                env=env,
                cwd=directory,
                timeout=10,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        credentials = dict(line.split("=", 1) for line in result.stdout.splitlines())
        self.assertEqual(credentials["username"], "x-access-token")
        self.assertEqual(credentials["password"], "test-workflow-token")


if __name__ == "__main__":
    unittest.main()
