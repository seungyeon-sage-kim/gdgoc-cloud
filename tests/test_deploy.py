"""The deploy's order and its way back (scripts/deploy.sh), against a pretend Cloud Run.

The failure paths are the point of the script and a real deploy rarely takes them, so
they run here: a small stand-in for gcloud keeps each service's revisions and traffic
in a file, and each test looks at what every service serves in the end.
"""

import json
import os
import re
import stat
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
WEB = "my-web"
STAGES = ["record", "stage", "smoke", "switch", "verify"]

FAKE_GCLOUD = r"""
import json, os, sys

args = sys.argv[1:]
line = " ".join(args)
path = os.environ["FAKE_CLOUD"]
cloud = json.load(open(path))
cloud["calls"].append(line)


def save():
    json.dump(cloud, open(path, "w"))


def flag(name):
    return args[args.index(name) + 1]


def latest(service):
    return service["revisions"][-1]["name"]


if os.environ.get("FAIL_ON") and os.environ["FAIL_ON"] in line:
    save()
    sys.exit(1)

kind, verb, name = args[1], args[2], args[3]
if kind == "jobs":
    if verb == "describe":
        print(json.dumps({"spec": {"template": {"spec": {"template": {"spec": {"containers": [{"image": cloud["job"]}]}}}}}}))
    elif verb == "update":
        cloud["job"] = flag("--image")
    elif verb == "execute":
        run_args = [a.split("=", 1)[1] for a in args if a.startswith("--args=")]
        cloud["job_runs"].append(" ".join([cloud["job"], *run_args]))
else:
    service = cloud["services"][name]
    if verb == "describe":
        if "value(status.url)" in args:
            print(f"https://{name}.run.app")
        else:
            traffic = [{"revisionName": r, "percent": p} for r, p in service["traffic"].items()]
            traffic += [{"revisionName": latest(service), "tag": "candidate", "url": f"https://candidate---{name}.run.app"}]
            print(json.dumps({"status": {"traffic": traffic}}))
    elif verb == "update":
        assert "--no-traffic" in args, "a new revision must start with no traffic"
        number = len(service["revisions"]) + 1
        service["revisions"].append({"name": f"{name}-{number}", "image": flag("--image")})
    elif verb == "update-traffic":
        if "--to-latest" in args:
            service["traffic"] = {latest(service): 100}
        else:
            service["traffic"] = {p.split("=")[0]: int(p.split("=")[1]) for p in flag("--to-revisions").split(",")}
save()
"""

FAKE_CURL = r"""
import json, os, sys

url = next(a for a in sys.argv[1:] if a.startswith("https://"))
path = os.environ["FAKE_CLOUD"]
cloud = json.load(open(path))
cloud["calls"].append(f"curl {url}")
json.dump(cloud, open(path, "w"))
sys.exit(1 if os.environ.get("FAIL_ON") and os.environ["FAIL_ON"] in url else 0)
"""


@pytest.fixture
def cloud(tmp_path):
    """Three services and a job, all on the version called old."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, source in (("gcloud", FAKE_GCLOUD), ("curl", FAKE_CURL)):
        tool = bin_dir / name
        tool.write_text(f"#!{sys.executable}\n{source}")
        tool.chmod(tool.stat().st_mode | stat.S_IEXEC)

    def service(name):
        return {"revisions": [{"name": f"{name}-1", "image": "old"}], "traffic": {f"{name}-1": 100}}

    state = tmp_path / "cloud.json"
    state.write_text(
        json.dumps(
            {
                "calls": [],
                "job": "old",
                "job_runs": [],
                "services": {name: service(name) for name in ("ticket-worker", "waitlist-promoter", WEB)},
            }
        )
    )
    return Cloud(state, tmp_path, bin_dir)


class Cloud:
    def __init__(self, state, tmp_path, bin_dir):
        self.state, self.tmp_path, self.bin_dir = state, tmp_path, bin_dir

    def run(self, stage, fail_on=None):
        env = {
            "PATH": f"{self.bin_dir}{os.pathsep}{os.environ['PATH']}",
            "FAKE_CLOUD": str(self.state),
            "RUNNER_TEMP": str(self.tmp_path),
            "GITHUB_OUTPUT": str(self.tmp_path / "output"),
            "REGION": "asia-northeast3",
            "IMAGES": "registry",
            "WEB_SERVICE": WEB,
            "GITHUB_SHA": "new",
        }
        if fail_on:
            env["FAIL_ON"] = fail_on
        return subprocess.run(["bash", str(REPO / "scripts/deploy.sh"), stage], env=env, capture_output=True, text=True)

    def deploy(self, fail_on=None) -> str | None:
        """Like the workflow: stop at the first failed stage, then roll back. Returns that stage."""
        for stage in STAGES:
            if self.run(stage, fail_on).returncode != 0:
                done = self.run("rollback")
                assert done.returncode == 0, done.stderr
                return stage
        return None

    @property
    def data(self):
        return json.loads(self.state.read_text())

    def versions(self) -> dict:
        """Which version each part serves: 'old', 'new', or a split during a canary."""
        data = self.data
        found = {}
        for name, service in data["services"].items():
            images = {r["name"]: r["image"] for r in service["revisions"]}
            serving = {images[revision].split(":")[-1] for revision, percent in service["traffic"].items() if percent}
            found[name] = serving.pop() if len(serving) == 1 else "split"
        found["housekeeping"] = data["job"].split(":")[-1]
        return found

    def position(self, text: str) -> int:
        return next(i for i, call in enumerate(self.data["calls"]) if text in call)


ALL_OLD = {"ticket-worker": "old", "waitlist-promoter": "old", WEB: "old", "housekeeping": "old"}
ALL_NEW = {"ticket-worker": "new", "waitlist-promoter": "new", WEB: "new", "housekeeping": "new"}


def test_a_good_deploy_moves_everything_receivers_first(cloud):
    assert cloud.deploy() is None
    assert cloud.versions() == ALL_NEW
    assert cloud.data["services"][WEB]["revisions"][-1]["image"] == "registry/web:new"
    assert cloud.data["job_runs"] == ["registry/housekeeping:new --check"]  # tried once, changing nothing

    # Each move depends on the one before it having worked
    order = [
        "curl https://candidate---",
        "update-traffic ticket-worker",
        "update-traffic waitlist-promoter",
        "jobs update",
        "jobs execute",
        f"update-traffic {WEB}",
        f"curl https://{WEB}.run.app",
    ]
    positions = [cloud.position(step) for step in order]
    assert positions == sorted(positions)


def test_nothing_serves_the_new_version_until_the_switch(cloud):
    for stage in ("record", "stage", "smoke"):
        assert cloud.run(stage).returncode == 0
    assert cloud.versions() == ALL_OLD
    assert not [call for call in cloud.data["calls"] if "update-traffic" in call or "jobs update" in call]


@pytest.mark.parametrize(
    ("fail_on", "failed_stage"),
    [
        ("services update ticket-worker", "stage"),
        ("services update waitlist-promoter", "stage"),
        (f"services update {WEB}", "stage"),
        ("https://candidate---", "smoke"),
        ("update-traffic ticket-worker", "switch"),
        ("update-traffic waitlist-promoter", "switch"),
        ("jobs update", "switch"),
        ("jobs execute", "switch"),
        (f"update-traffic {WEB}", "switch"),
        (f"https://{WEB}.run.app", "verify"),
    ],
)
def test_any_failure_leaves_everything_on_the_old_version(cloud, fail_on, failed_stage):
    assert cloud.deploy(fail_on=fail_on) == failed_stage
    assert cloud.versions() == ALL_OLD


def test_a_failed_smoke_test_never_touches_the_job(cloud):
    cloud.deploy(fail_on="https://candidate---")
    assert cloud.data["job_runs"] == []
    assert not [call for call in cloud.data["calls"] if "--to-latest" in call]


def test_rollback_restores_a_canary_split_exactly(cloud):
    data = cloud.data
    web = data["services"][WEB]
    web["revisions"].append({"name": f"{WEB}-2", "image": "canary"})
    web["traffic"] = {f"{WEB}-1": 90, f"{WEB}-2": 10}
    cloud.state.write_text(json.dumps(data))

    assert cloud.deploy(fail_on=f"https://{WEB}.run.app") == "verify"
    assert cloud.data["services"][WEB]["traffic"] == {f"{WEB}-1": 90, f"{WEB}-2": 10}


def test_a_failed_record_leaves_nothing_to_roll_back(cloud):
    assert cloud.deploy(fail_on="jobs describe") == "record"
    assert cloud.versions() == ALL_OLD
    assert not [call for call in cloud.data["calls"] if "update" in call]


def test_a_strange_answer_stops_the_deploy_before_anything_moves(cloud):
    data = cloud.data
    data["job"] = None  # recording this would make rollback ask for an image called "null"
    cloud.state.write_text(json.dumps(data))

    assert cloud.deploy() == "record"
    assert not [call for call in cloud.data["calls"] if "update" in call]


def test_rollback_keeps_going_past_a_failure_and_reports_it(cloud):
    for stage in STAGES:
        assert cloud.run(stage).returncode == 0
    assert cloud.versions() == ALL_NEW

    result = cloud.run("rollback", fail_on="update-traffic waitlist-promoter")
    assert result.returncode != 0
    assert cloud.versions() == ALL_NEW | {"ticket-worker": "old", WEB: "old", "housekeeping": "old"}


def test_the_workflow_runs_the_stages_in_order_and_rolls_back_on_failure():
    workflow = (REPO / ".github/workflows/ci.yml").read_text()
    assert re.findall(r"run: scripts/deploy\.sh (\w+)", workflow) == [*STAGES, "rollback"]
    assert re.search(r"if: failure\(\) \|\| cancelled\(\)\n\s+run: scripts/deploy\.sh rollback", workflow)
