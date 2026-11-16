"""Terraform in CI (scripts/infra.sh), against a pretend terraform and gh.

The script runs from a copy in a temporary repository, because it writes
terraform.tfvars next to itself and the real one must stay untouched.
"""

import json
import os
import re
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent

FAKE_TERRAFORM = r"""
import os, sys

with open(os.environ["CALLS"], "a") as calls:
    calls.write(" ".join(sys.argv[1:]) + "\n")
if sys.argv[1] == "show":
    print(os.environ["PLAN_JSON"])
sys.exit(1 if os.environ.get("FAIL_ON") == sys.argv[1] else 0)
"""

FAKE_GH = r"""
import os, sys

if os.environ.get("GH_FAILS"):
    sys.exit(1)
print(os.environ["LAST_SUCCESS"])
"""


def plan_json(*actions):
    return json.dumps(
        {"resource_changes": [{"address": f"thing.{i}", "change": {"actions": a}} for i, a in enumerate(actions)]}
    )


class Repo:
    def __init__(self, path):
        self.path = path
        (path / "scripts").mkdir()
        (path / "infra").mkdir()
        (path / "bin").mkdir()
        shutil.copy(REPO / "scripts/infra.sh", path / "scripts/infra.sh")
        (path / "infra/run.tf").write_text("first\n")
        (path / "app.py").write_text("first\n")
        for name, source in (("terraform", FAKE_TERRAFORM), ("gh", FAKE_GH)):
            tool = path / "bin" / name
            tool.write_text(f"#!{sys.executable}\n{source}")
            tool.chmod(tool.stat().st_mode | stat.S_IEXEC)
        self.git("init", "-q")
        self.first = self.commit("first")

    def git(self, *args):
        identity = ["-c", "user.name=t", "-c", "user.email=t@example.com"]
        done = subprocess.run(["git", *identity, *args], cwd=self.path, check=True, capture_output=True, text=True)
        return done.stdout.strip()

    def commit(self, message):
        self.git("add", "scripts", "infra", "app.py")
        self.git("commit", "-q", "-m", message)
        return self.git("rev-parse", "HEAD")

    def run(self, stage, **env):
        env = {
            "PATH": f"{self.path / 'bin'}{os.pathsep}{os.environ['PATH']}",
            "CALLS": str(self.path / "calls"),
            "GITHUB_OUTPUT": str(self.path / "output"),
            "GITHUB_EVENT_NAME": "push",
            "PROJECT_ID": "my-project",
            "TFVARS": 'project_id = "my-project"',
            "LAST_SUCCESS": self.first,
            "PLAN_JSON": plan_json(),
            **env,
        }
        return subprocess.run(
            ["bash", str(self.path / "scripts/infra.sh"), stage], env=env, capture_output=True, text=True
        )

    def applies(self, **env) -> bool:
        done = self.run("changes", **env)
        assert done.returncode == 0, done.stderr
        return (self.path / "output").read_text().strip() == "infra=true"

    @property
    def calls(self) -> list[str]:
        return (self.path / "calls").read_text().splitlines()


@pytest.fixture
def repo(tmp_path):
    return Repo(tmp_path)


def test_a_push_that_leaves_infra_alone_applies_nothing(repo):
    (repo.path / "app.py").write_text("second\n")
    repo.commit("code only")
    assert not repo.applies()


def test_a_push_that_changes_infra_applies(repo):
    (repo.path / "infra/run.tf").write_text("second\n")
    repo.commit("settings")
    assert repo.applies()


def test_a_failed_apply_is_tried_again_by_the_next_push(repo):
    (repo.path / "infra/run.tf").write_text("second\n")
    repo.commit("settings: its run failed, so it is not the last success")
    (repo.path / "app.py").write_text("second\n")
    repo.commit("code only")
    assert repo.applies()


def test_a_run_started_by_a_person_always_applies(repo):
    assert repo.applies(GITHUB_EVENT_NAME="workflow_dispatch")


@pytest.mark.parametrize("last", ["", "0" * 40], ids=["no successful run yet", "a commit that is gone"])
def test_applies_when_there_is_nothing_to_compare_with(repo, last):
    assert repo.applies(LAST_SUCCESS=last)


def test_a_failed_lookup_stops_the_job_instead_of_guessing(repo):
    assert repo.run("changes", GH_FAILS="1").returncode != 0


def test_plan_uses_the_shared_state_and_the_settings_from_github(repo):
    assert repo.run("plan").returncode == 0
    assert (repo.path / "infra/terraform.tfvars").read_text() == 'project_id = "my-project"\n'
    assert "bucket=my-project-tfstate" in repo.calls[0]
    assert "prefix=gdgoc-tickets" in repo.calls[0]
    assert "-out=tfplan" in repo.calls[1]


@pytest.mark.parametrize("actions", [(), (["no-op"], ["create"], ["update"])], ids=["no changes", "adds and updates"])
def test_a_plan_that_destroys_nothing_passes(repo, actions):
    assert repo.run("check", PLAN_JSON=plan_json(*actions)).returncode == 0


@pytest.mark.parametrize(
    "doomed", [["delete"], ["delete", "create"], ["create", "delete"]], ids=["delete", "replace", "replace, new first"]
)
def test_a_plan_that_destroys_anything_stops_and_names_it(repo, doomed):
    done = repo.run("check", PLAN_JSON=plan_json(["create"], doomed))
    assert done.returncode != 0
    assert "thing.1" in done.stdout
    assert "thing.0" not in done.stdout


def test_a_person_can_allow_a_destroy_and_it_is_still_named(repo):
    done = repo.run("check", PLAN_JSON=plan_json(["create"], ["delete"]), ALLOW_DESTROY="true")
    assert done.returncode == 0
    assert "thing.1" in done.stdout
    assert repo.run("check", PLAN_JSON=plan_json(["delete"]), ALLOW_DESTROY="").returncode != 0  # a push


def test_a_plan_that_cannot_be_read_stops(repo):
    assert repo.run("check", FAIL_ON="show").returncode != 0


def test_apply_applies_the_saved_plan_and_reports_failure(repo):
    assert repo.run("apply").returncode == 0
    assert repo.calls == ["apply -input=false -no-color tfplan"]
    assert repo.run("apply", FAIL_ON="apply").returncode != 0


def workflow_job(name: str) -> str:
    workflow = (REPO / ".github/workflows/ci.yml").read_text()
    return re.search(rf"^  {name}:\n.*?(?=^  \w[\w-]*:\n|\Z)", workflow, re.M | re.S).group()


def test_the_workflow_applies_before_the_deploy_and_a_failure_stops_the_deploy():
    infra, deploy = workflow_job("infra"), workflow_job("deploy")
    assert re.findall(r"run: scripts/infra\.sh (\w+)", infra) == ["changes", "plan", "check", "apply"]
    assert "needs: [build, infra]" in deploy
    assert "needs.infra.result != 'failure'" in deploy


def test_only_the_infra_job_uses_the_terraform_login():
    workflow = (REPO / ".github/workflows/ci.yml").read_text()
    assert workflow.count("service_account: ${{ vars.TERRAFORM_SA }}") == 1
    assert "service_account: ${{ vars.TERRAFORM_SA }}" in workflow_job("infra")
    assert "vars.DEPLOYER_SA" not in workflow_job("infra")


def test_the_image_scan_stops_a_deploy_only_with_production_extras():
    scan = workflow_job("image-scan")
    assert "exit-code: ${{ vars.PRODUCTION_EXTRAS == 'true' && '1' || '0' }}" in scan
    assert "PRODUCTION_EXTRAS = tostring(var.production_extras)" in (REPO / "infra/outputs.tf").read_text()
