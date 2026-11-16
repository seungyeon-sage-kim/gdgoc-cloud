"""The lab script (./lab) against a pretend Google Cloud.

The checks decide whether a student's hand-built step passes, so each one is tried with the
right settings and with the mistakes students make: the wrong identity, a missing grant, an
unsigned push. The shortcuts must never make twice what a student already made by hand.
"""

import importlib.machinery
import importlib.util
import json
import os
import re
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent


def load_lab(home):
    os.environ["LAB_HOME"] = str(home)  # never the real ~/.gdgoc-lab
    loader = importlib.machinery.SourceFileLoader("lab", str(REPO / "lab"))
    spec = importlib.util.spec_from_loader("lab", loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    del os.environ["LAB_HOME"]
    assert module.TFVARS == home / "terraform.tfvars"
    return module


@pytest.fixture
def lab(tmp_path, monkeypatch):
    module = load_lab(tmp_path / "home")
    monkeypatch.setattr(module, "gh", lambda *args: None)
    return module


P = "student-1"
N = "123456789012"


def sa(key):
    return f"tickets-{key}@{P}.iam.gserviceaccount.com"


def make_cloud(lab, gcloud=None, api=None, policies=None):
    """A cloud whose answers come from dicts: gcloud by its arguments, the Cloud Run API by path."""
    gcloud, api, policies = gcloud or {}, api or {}, policies or {}

    class Fake(lab.Cloud):
        def gcloud(self, *args, project=True):
            return gcloud.get(" ".join(args))

        def run_api(self, path):
            return api.get(path)

        def policy(self, kind, name):
            return policies.get((kind, name), {})

    cloud = Fake(P, "asia-northeast3", "student@example.com")
    cloud._number = N
    return cloud


def bindings(*pairs):
    return {"bindings": [{"role": role, "members": [member]} for role, member in pairs]}


def good_web(lab):
    cloud = make_cloud(lab)
    env = [{"name": k, "value": v} for k, v in lab.env_web(cloud).items()]
    env += [
        {
            "name": "TICKET_SECRET",
            "valueSource": {"secretKeyRef": {"secret": f"projects/{P}/secrets/ticket-secret", "version": "1"}},
        },
        {"name": "SESSION_SECRET", "valueSource": {"secretKeyRef": {"secret": "session-secret", "version": "1"}}},
    ]
    return {
        "terminalCondition": {"state": "CONDITION_SUCCEEDED"},
        "ingress": "INGRESS_TRAFFIC_ALL",
        "invokerIamDisabled": True,
        "scaling": {"maxInstanceCount": 20},
        "uri": "http://127.0.0.1:9",  # nothing answers: the site check fails on its own line
        "template": {
            "serviceAccount": sa("web"),
            "maxInstanceRequestConcurrency": 40,
            "containers": [{"image": "x/web:lab", "env": env, "startupProbe": {"httpGet": {"path": "/health"}}}],
        },
    }


def web_policies():
    reader = "roles/secretmanager.secretAccessor"
    return {
        ("secret", "ticket-secret"): bindings((reader, f"serviceAccount:{sa('web')}")),
        ("secret", "session-secret"): bindings((reader, f"serviceAccount:{sa('web')}")),
    }


def run(lab, check, cloud):
    result = lab.Result()
    check(cloud, result)
    return result


def problems(result):
    return [text for kind, text in result.lines if kind == "NOT"]


# ---------- settings


def test_settings_round_trip_and_parts_keep_the_order_of_lab_tf(lab):
    values = {"project_id": P, "github_repo_id": "0", "admin_emails": ["a@example.com"], "lab_parts": ["start"]}
    lab.write_tfvars(values)
    assert lab.read_tfvars() == values
    assert lab.add_parts(values, ["workers", "base"]) == ["start", "base", "workers"]


def test_lab_parts_names_match_terraform():
    lab_tf = (REPO / "infra/lab.tf").read_text()
    names = re.search(r"lab_part_names = \[(.*?)\]", lab_tf).group(1)
    assert [n.strip(' "') for n in names.split(",")] == load_lab(Path("/nonexistent/home")).PARTS


def test_a_real_tfvars_file_is_left_alone(lab, tmp_path, monkeypatch):
    infra = tmp_path / "infra"
    infra.mkdir()
    (infra / "terraform.tfvars").write_text('project_id = "production"\n')
    monkeypatch.setattr(lab, "INFRA", infra)
    with pytest.raises(SystemExit):
        lab.link_settings()
    assert (infra / "terraform.tfvars").read_text() == 'project_id = "production"\n'


def test_settings_are_linked_into_any_clone(lab, tmp_path, monkeypatch):
    infra = tmp_path / "infra"
    infra.mkdir()
    monkeypatch.setattr(lab, "INFRA", infra)
    lab.link_settings()
    assert (infra / "terraform.tfvars").readlink() == lab.HOME / "terraform.tfvars"


def test_the_pasted_settings_match_terraform(lab):
    """A web or job built by hand gets the same settings as one Terraform builds."""
    run_tf = (REPO / "infra/run.tf").read_text()
    common = re.search(r"common_env = \{(.*?)\n  \}", run_tf, re.S).group(1)
    names = set(re.findall(r"^\s+([A-Z_]+)\s+=", common, re.M)) - {"STAFF_EMAILS"}  # empty in the lab
    cloud = make_cloud(lab)
    assert names <= set(lab.env_web(cloud)) and names <= set(lab.env_job(cloud))
    job_names = set(re.findall(r"^\s+(WEB_SERVICE|WEB_MIN_INSTANCES|PREWARM_\w+)\s+=", run_tf, re.M))
    assert job_names == set(lab.env_job(cloud)) - set(lab.common_env(cloud))


def test_every_role_in_the_permission_table_is_one_terraform_grants(lab):
    terraform = "".join(p.read_text() for p in (REPO / "infra").glob("*.tf"))
    for _, _, _, role, _, _ in lab.DESIGN:
        assert role.split("/")[-1] in terraform, role


# ---------- checks


def test_web_built_as_the_sheet_says_passes_except_the_offline_site(lab):
    cloud = make_cloud(lab, api={"services/web": good_web(lab)}, policies=web_policies())
    assert [p for p in problems(run(lab, lab.check_web, cloud)) if "does not answer" not in p] == []


def test_web_on_the_default_identity_fails_and_says_where_to_fix_it(lab):
    web = good_web(lab)
    web["template"]["serviceAccount"] = f"{N}-compute@developer.gserviceaccount.com"
    cloud = make_cloud(lab, api={"services/web": web}, policies=web_policies())
    assert any("not tickets-web (Security tab)" in p for p in problems(run(lab, lab.check_web, cloud)))


def test_web_without_its_secret_grant_fails_on_that_secret(lab):
    policies = web_policies()
    del policies[("secret", "session-secret")]
    cloud = make_cloud(lab, api={"services/web": good_web(lab)}, policies=policies)
    found = problems(run(lab, lab.check_web, cloud))
    assert any("may not read session-secret" in p for p in found)
    assert not any("may not read ticket-secret" in p for p in found)


def test_a_missing_web_is_not_built_yet_rather_than_failed(lab):
    status, lines = lab.run_check(make_cloud(lab), "web")
    assert status == "-" and "no service web yet" in lines[0][1]


WORKER = f"https://ticket-worker-{N}.asia-northeast3.run.app"
SIGNED_PUSH = {"pushEndpoint": WORKER, "oidcToken": {"serviceAccountEmail": sa("pubsub-push")}}


def worker_subscription(push):
    sub = {"topic": f"projects/{P}/topics/registrations", "ackDeadlineSeconds": 60}
    if push:
        sub["pushConfig"] = push
    return sub


def test_an_unsigned_push_fails_and_names_the_setting(lab):
    cloud = make_cloud(
        lab,
        gcloud={
            "pubsub subscriptions describe ticket-worker": worker_subscription(
                {"pushEndpoint": f"https://ticket-worker-{N}.asia-northeast3.run.app"}
            )
        },
        api={"services/ticket-worker": {"uri": f"https://ticket-worker-{N}.asia-northeast3.run.app"}},
    )
    assert problems(run(lab, lab.check_push, cloud)) == [
        "pushes are not signed as tickets-pubsub-push (Enable authentication)"
    ]


def test_a_signed_push_to_either_address_of_the_worker_passes(lab):
    push = {
        "pushEndpoint": "https://ticket-worker-abc123-du.a.run.app/",
        "oidcToken": {"serviceAccountEmail": sa("pubsub-push")},
    }
    cloud = make_cloud(
        lab,
        gcloud={"pubsub subscriptions describe ticket-worker": worker_subscription(push)},
        api={"services/ticket-worker": {"uri": "https://ticket-worker-abc123-du.a.run.app"}},
    )
    assert problems(run(lab, lab.check_push, cloud)) == []


def test_a_pull_subscription_has_not_reached_step_8(lab):
    cloud = make_cloud(lab, gcloud={"pubsub subscriptions describe ticket-worker": worker_subscription(None)})
    assert lab.run_check(cloud, "push")[0] == "-"


def test_the_scheduler_made_from_the_job_page_passes(lab):
    """The Console's Add scheduler trigger writes the older v1 address; both start the same job."""
    job = {
        "template": {
            "template": {
                "serviceAccount": sa("housekeeping"),
                "maxRetries": 0,
                "timeout": "240s",
                "containers": [
                    {"env": [{"name": "WEB_SERVICE", "value": f"projects/{P}/locations/asia-northeast3/services/web"}]}
                ],
            }
        }
    }
    scheduler = {
        "name": f"projects/{P}/locations/asia-northeast3/jobs/housekeeping",
        "schedule": "*/5 * * * *",
        "timeZone": "Asia/Seoul",
        "state": "ENABLED",
        "httpTarget": {
            "uri": f"https://asia-northeast3-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/{P}/jobs/housekeeping:run",
            "oauthToken": {"serviceAccountEmail": sa("scheduler")},
        },
    }
    cloud = make_cloud(
        lab,
        gcloud={"scheduler jobs list --location asia-northeast3": [scheduler]},
        api={"jobs/housekeeping": job},
        policies={("job", "housekeeping"): bindings(("roles/run.invoker", f"serviceAccount:{sa('scheduler')}"))},
    )
    assert problems(run(lab, lab.check_schedule, cloud)) == []
    assert lab.run_check(cloud, "paused")[0] == "-"  # still running, so not paused yet
    scheduler["state"] = "PAUSED"
    assert lab.run_check(cloud, 26)[0] == "ok"  # the old step number still works


def test_a_trust_open_to_every_repository_fails(lab, monkeypatch):
    monkeypatch.setattr(lab, "own_repo", lambda: "student/gdgoc-tickets")
    monkeypatch.setattr(lab, "gh", lambda *args: "555" if args[:2] == ("api", "repos/student/gdgoc-tickets") else None)
    pool = "principalSet://iam.googleapis.com/projects/123456789012/locations/global/workloadIdentityPools/github"
    cloud = make_cloud(
        lab,
        gcloud={
            "iam workload-identity-pools describe github --location global": {"state": "ACTIVE"},
            "iam workload-identity-pools providers describe gdgoc-tickets --workload-identity-pool github --location global": {
                "oidc": {"issuerUri": "https://token.actions.githubusercontent.com"},
                "attributeMapping": {
                    "attribute.repository_id": "assertion.repository_id",
                    "attribute.ref": "assertion.ref",
                },
                "attributeCondition": "assertion.repository_id == '555' && assertion.ref == 'refs/heads/main'",
            },
        },
        policies={
            ("sa", sa("deployer")): {
                "bindings": [
                    {
                        "role": "roles/iam.workloadIdentityUser",
                        "members": [f"{pool}/attribute.repository_id/555", f"{pool}/*"],
                    }
                ]
            }
        },
    )
    found = problems(run(lab, lab.check_trust, cloud))
    assert any(p.startswith(f"  also allowed: {pool}/*") for p in found)
    assert not any("may not act as tickets-deployer" in p for p in found)


def test_a_database_in_the_wrong_region_says_it_cannot_be_moved(lab):
    cloud = make_cloud(
        lab,
        gcloud={
            "firestore databases describe --database=(default)": {
                "type": "FIRESTORE_NATIVE",
                "locationId": "us-central1",
            }
        },
    )
    assert any("can't be changed" in p for p in problems(run(lab, lab.check_database, cloud)))


def test_skipped_steps_show_in_the_check(lab):
    lab.HOME.mkdir(parents=True)
    lab.record_skip(8)
    cloud = make_cloud(lab, gcloud={"pubsub subscriptions describe ticket-worker": worker_subscription(None)})
    assert lab.run_check(cloud, "push")[0] == "- (skipped)"


# ---------- shortcuts take over what a student already made


def test_takeover_imports_only_what_exists_and_is_not_in_terraform(lab, monkeypatch):
    calls = []

    def fake_terraform(*args, capture=True, env=None):
        calls.append(args)
        out = "google_pubsub_topic.registrations[0]\n" if args[0] == "state" else ""
        return subprocess.CompletedProcess(args, 0, out, "")

    monkeypatch.setattr(lab, "terraform", fake_terraform)
    existing = {"pubsub topics describe registrations": {}, "pubsub topics describe emails": {}}
    lab.adopt(make_cloud(lab, gcloud=existing), ["topics"], env={})
    imports = [c[3:] for c in calls if c[0] == "import"]
    assert imports == [("google_pubsub_topic.emails[0]", f"projects/{P}/topics/emails")]


def test_a_shortcut_keeps_a_push_the_student_made_on_a_terraform_subscription(lab, monkeypatch):
    lab.write_tfvars(
        {
            "project_id": P,
            "github_repo_id": "0",
            "admin_emails": ["a@example.com"],
            "lab_parts": ["start", "base", "topics"],
        }
    )
    monkeypatch.setattr(lab, "terraform_env", lambda: {})
    monkeypatch.setattr(lab, "ensure_terraform", lambda env: None)
    monkeypatch.setattr(lab, "adopt", lambda cloud, parts, env: None)
    monkeypatch.setattr(lab, "terraform", lambda *a, **k: subprocess.CompletedProcess(a, 0, "", ""))
    pushed = {"pubsub subscriptions describe ticket-worker": worker_subscription(SIGNED_PUSH)}
    lab.apply(make_cloud(lab, gcloud=pushed), ["workers"])
    assert lab.read_tfvars()["lab_parts"] == ["start", "base", "topics", "workers", "push"]


def test_an_unfinished_push_is_not_completed_by_terraform(lab, monkeypatch):
    """Pushing without authentication is the student's half-done switch: Terraform must not finish it."""
    lab.write_tfvars({"project_id": P, "github_repo_id": "0", "admin_emails": [], "lab_parts": ["start", "topics"]})
    for name in ("terraform_env", "ensure_terraform", "adopt"):
        monkeypatch.setattr(lab, name, lambda *a, **k: {})
    monkeypatch.setattr(lab, "terraform", lambda *a, **k: subprocess.CompletedProcess(a, 0, "", ""))
    unsigned = {"pubsub subscriptions describe ticket-worker": worker_subscription({"pushEndpoint": WORKER})}
    lab.apply(make_cloud(lab, gcloud=unsigned), ["workers"])
    assert "push" not in lab.read_tfvars()["lab_parts"]


# ---------- ./lab up


def test_up_builds_everything_but_push_on_a_first_build(lab):
    values = {"lab_parts": ["start"]}
    cloud = make_cloud(lab, gcloud={"pubsub subscriptions describe ticket-worker": worker_subscription(None)})
    assert lab.up_parts(values, cloud) == lab.UP_PARTS
    assert lab.up_parts(values, make_cloud(lab)) == lab.UP_PARTS  # no subscription yet
    assert "push" not in lab.UP_PARTS and "trust" not in lab.UP_PARTS
    assert set(lab.UP_PARTS) | {"push", "trust", "deployer"} == set(lab.PARTS)


def test_up_keeps_the_push_a_student_switched_on(lab):
    values = {"lab_parts": lab.UP_PARTS}
    pushed = make_cloud(lab, gcloud={"pubsub subscriptions describe ticket-worker": worker_subscription(SIGNED_PUSH)})
    assert "push" in lab.up_parts(values, pushed)
    unsigned = make_cloud(
        lab, gcloud={"pubsub subscriptions describe ticket-worker": worker_subscription({"pushEndpoint": WORKER})}
    )
    assert "push" not in lab.up_parts(values, unsigned)


def test_up_keeps_github_parts_once_built(lab):
    values = {"lab_parts": [*lab.UP_PARTS, "push", "trust", "deployer"]}
    assert lab.up_parts(values, make_cloud(lab)) == lab.PARTS


def github_cloud(lab, monkeypatch, folder):
    """./lab github with a built web, the folder cloned from FOLDER, and every gh call recorded."""
    cloud = make_cloud(lab, api={"services/web": {"uri": "https://web"}})
    cloud._number = N
    calls, applied = [], []

    def fake_gh(*args):
        calls.append(args)
        return "987654" if args[:2] == ("api", "repos/student/gdgoc-tickets") else None

    lab.write_tfvars({"project_id": P, "github_repo_id": "0", "lab_parts": list(lab.UP_PARTS)})
    monkeypatch.setattr(lab, "cloud_from_settings", lambda: cloud)
    monkeypatch.setattr(lab, "github_login", lambda: None)
    monkeypatch.setattr(lab, "folder_repo", lambda: folder)
    monkeypatch.setattr(lab, "made_from_template", lambda repo: repo == "student/gdgoc-tickets")
    monkeypatch.setattr(lab, "gh", fake_gh)
    monkeypatch.setattr(lab, "apply", lambda cloud, parts: applied.append(parts))
    return calls, applied


def test_github_builds_the_trust_and_leaves_the_variables_and_the_test_to_the_student(lab, monkeypatch, capsys):
    calls, applied = github_cloud(lab, monkeypatch, "student/gdgoc-tickets")
    lab.github()
    assert lab.read_tfvars()["github_repo_id"] == "987654"
    assert applied == [lab.SKIPS[15]] and {"trust", "deployer"} <= set(applied[0])
    printed = capsys.readouterr().out
    for line in (f"GCP_PROJECT_ID  {P}", f"DEPLOYER_SA     {sa('deployer')}", "WIF_PROVIDER    projects/"):
        assert line in printed
    assert "Run workflow" in printed
    # The student adds the variables and runs the sign-in test; ./lab does neither
    assert not [c for c in calls if c[:2] in (("variable", "set"), ("workflow", "run"))]


@pytest.mark.parametrize("folder", ["joyrla/gdgoc-tickets-lab", "someone/other", None])
def test_github_outside_the_students_copy_says_how_to_make_one(lab, monkeypatch, folder):
    _, applied = github_cloud(lab, monkeypatch, folder)
    with pytest.raises(SystemExit):
        lab.github()
    assert applied == [] and lab.read_tfvars()["github_repo_id"] == "0"


def test_up_without_an_id_uses_the_cloud_shell_project_once_confirmed(lab, monkeypatch):
    calls = []

    def fake_sh(cmd, capture=False, check=True, **kw):
        calls.append(cmd)
        return "airy-adapter-514904-p9\n" if cmd[:3] == ["gcloud", "config", "get-value"] else "realtestrun\n"

    monkeypatch.setattr(lab, "sh", fake_sh)
    monkeypatch.setattr("builtins.input", lambda prompt: "y")
    assert lab.chosen_project() == "airy-adapter-514904-p9"
    monkeypatch.setattr("builtins.input", lambda prompt: "")
    with pytest.raises(SystemExit):
        lab.chosen_project()
    # Once the lab is set up, its own project, with no question
    lab.write_tfvars({"project_id": P, "lab_parts": ["start"]})
    monkeypatch.setattr("builtins.input", lambda prompt: pytest.fail("asked again"))
    assert lab.chosen_project() == P


def test_a_web_still_on_the_sample_image_is_not_a_failure(lab):
    sample = {"template": {"containers": [{"image": "us-docker.pkg.dev/cloudrun/container/hello"}]}}
    real = {"template": {"containers": [{"image": "asia-northeast3-docker.pkg.dev/p/gdgoc-tickets/web:3f1bb83"}]}}
    assert lab.placeholder(sample) and not lab.placeholder(real) and not lab.placeholder({})


def stale_lock(lab, monkeypatch, answer):
    (lab.HOME / "backend.hcl").parent.mkdir(parents=True, exist_ok=True)
    (lab.HOME / "backend.hcl").write_text('bucket = "p-tfstate"\nprefix = "gdgoc-tickets"\n')
    lock = {"ID": "1763270000000000", "Who": "student@cs-123-default", "Created": "2026-11-16T05:40:00.0Z"}
    reads, unlocked = [], []

    def fake_run(cmd, **kw):
        reads.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(lock), stderr="")

    def fake_terraform(*args, **kw):
        unlocked.append(args)
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr(lab.subprocess, "run", fake_run)
    monkeypatch.setattr(lab, "terraform", fake_terraform)
    monkeypatch.setattr("builtins.input", lambda prompt: answer)
    return reads, unlocked


def test_a_lock_left_by_a_stopped_run_is_removed_once_the_student_agrees(lab, monkeypatch):
    reads, unlocked = stale_lock(lab, monkeypatch, "y")
    lab.clear_stale_lock({})
    assert reads[0][-1] == "gs://p-tfstate/gdgoc-tickets/default.tflock"
    assert unlocked == [("force-unlock", "-force", "1763270000000000")]


def test_a_lock_is_kept_when_the_student_does_not_agree(lab, monkeypatch):
    _, unlocked = stale_lock(lab, monkeypatch, "")
    with pytest.raises(SystemExit):
        lab.clear_stale_lock({})
    assert unlocked == []


def test_a_refused_connection_to_a_google_api_is_tried_again(lab):
    # Cloud Shell, 2026-11-16: planning stopped on this, and a rerun went through
    error = (
        'Error: Error when reading or editing FirestoreDatabase: Get "https://firestore.googleapis.com/v1/x": '
        "dial tcp 64.233.189.95:443: connect: connection refused"
    )
    assert lab.RETRYABLE.search(error)


def test_the_billing_message_shows_what_gcloud_said_when_it_fails(lab):
    class Failing(lab.Cloud):
        def gcloud(self, *args, project=True):
            self.error = "ERROR: (gcloud.billing.projects.describe) PERMISSION_DENIED: not allowed"
            return None

    problem = lab.billing_problem(Failing(P))
    assert "PERMISSION_DENIED" in problem and "no billing account" not in problem
    unlinked = make_cloud(lab, gcloud={f"billing projects describe {P}": {"billingEnabled": False}})
    assert "no billing account linked" in lab.billing_problem(unlinked)
    linked = make_cloud(lab, gcloud={f"billing projects describe {P}": {"billingEnabled": True}})
    assert lab.billing_problem(linked) == ""


# ---------- ./lab event

NOW = datetime(2026, 11, 16, 14, 37, 12, tzinfo=timezone(timedelta(hours=9)))


def test_the_event_document_has_the_fields_of_an_event(lab):
    from shared.models import Event

    document, errors = lab.event_document("Rush-A", NOW)
    assert errors == []
    assert set(document) == set(Event.__dataclass_fields__)
    event = Event(**document)
    assert event.id == "rush-a" and event.published is True and event.poster_version == ""
    assert (event.seat_limit, event.waitlist_limit) == (200, 50)
    assert document["opens_at"] == "2026-11-16T12:37:00+09:00"  # open since 2 hours ago
    assert document["closes_at"] == "2026-11-17T23:00:00+09:00"
    assert document["starts_at"] == "2026-11-18T19:00:00+09:00"
    assert document["ends_at"] == "2026-11-18T21:00:00+09:00"
    assert document["confirm_deadline"] == "2026-11-18T09:00:00+09:00"
    assert document["created_at"] == "2026-11-16T14:37:12+09:00"
    assert event.opens < NOW < event.closes and event.deadline < event.starts < event.ends


def test_an_event_can_open_later(lab):
    document, errors = lab.event_document("opening", NOW, opens_in=20, seats=1, waitlist=0, title="오픈 테스트")
    assert errors == []
    assert document["opens_at"] == "2026-11-16T14:57:00+09:00"
    assert (document["seat_limit"], document["waitlist_limit"], document["title"]) == (1, 0, "오픈 테스트")
    assert lab.event_document("late", NOW, opens_in=60 * 48)[1]  # after signups close


def test_event_ids_follow_the_admin_form(lab):
    from web.admin import EVENT_ID

    for event_id in ("ab", "-abc", "a_b_c", "가나다", "a" * 41, "abc/def"):
        assert lab.event_document(event_id, NOW)[1], event_id
        assert not EVENT_ID.match(event_id.lower())
    for event_id in ("abc", "rush-a", "2026-night", "a" * 40):
        assert lab.event_document(event_id, NOW)[1] == []
        assert EVENT_ID.match(event_id)
    assert lab.event_document("abc", NOW, seats=0)[1] and lab.event_document("abc", NOW, waitlist=-1)[1]


def test_firestore_fields_keep_the_types(lab):
    assert lab.firestore_fields({"a": "x", "n": 3, "b": True, "e": ""}) == {
        "a": {"stringValue": "x"},
        "n": {"integerValue": "3"},
        "b": {"booleanValue": True},
        "e": {"stringValue": ""},
    }


# ---------- ./lab github


def test_the_sign_in_test_is_tried_again_when_the_permission_had_not_spread(lab, monkeypatch):
    runs, started = [{"databaseId": 1, "status": "completed", "conclusion": "success", "url": "u1"}], []

    def fake_gh(*args):
        if args[:2] == ("workflow", "run"):
            started.append(args)
            conclusion = "failure" if len(started) == 1 else "success"
            runs.insert(0, {"databaseId": len(runs) + 1, "status": "completed", "conclusion": conclusion,
                            "url": f"u{len(runs) + 1}"})  # fmt: skip
            return ""
        if args[:2] == ("run", "list"):
            return json.dumps(runs[:1])
        if args[:2] == ("run", "view"):
            return "Permission 'iam.serviceAccounts.getAccessToken' denied on resource (or it may not exist)."
        return None

    monkeypatch.setattr(lab, "gh", fake_gh)
    monkeypatch.setattr(lab.time, "sleep", lambda seconds: None)
    assert lab.sign_in_test("student/gdgoc-tickets") == (True, "u3")
    assert len(started) == 2


def test_a_sign_in_test_failing_for_another_reason_is_not_retried(lab, monkeypatch):
    started = []

    def fake_gh(*args):
        if args[:2] == ("workflow", "run"):
            started.append(args)
            return ""
        if args[:2] == ("run", "list"):
            return json.dumps(
                [{"databaseId": len(started), "status": "completed", "conclusion": "failure", "url": "u"}]
            )
        return "rejected by the attribute condition"

    monkeypatch.setattr(lab, "gh", fake_gh)
    monkeypatch.setattr(lab.time, "sleep", lambda seconds: None)
    assert lab.sign_in_test("student/gdgoc-tickets") == (False, "u")
    assert len(started) == 1


# ---------- ./lab drift


def test_drift_lists_real_changes_and_hides_the_revision_name(lab):
    web = {"template": [{"revision": "web-00007-abc", "max_instance_request_concurrency": 2}]}
    plan = {
        "resource_changes": [
            {"address": 'google_cloud_run_v2_service.svc["web"]', "type": "google_cloud_run_v2_service",
             "change": {"actions": ["update"], "before": web,
                        "after": {"template": [{"revision": None, "max_instance_request_concurrency": 40}]},
                        "after_unknown": {"template": [{}]}}},
            {"address": 'google_cloud_run_v2_service.svc["ticket-worker"]', "type": "google_cloud_run_v2_service",
             "change": {"actions": ["update"], "before": {"template": [{"revision": "w-1", "etag": "x"}]},
                        "after": {"template": [{"revision": None}]},
                        "after_unknown": {"template": [{"etag": True}]}}},
            {"address": "google_pubsub_topic.emails[0]", "type": "google_pubsub_topic",
             "change": {"actions": ["create"], "before": None, "after": {"name": "emails"}}},
            {"address": "google_project_service.apis", "type": "google_project_service",
             "change": {"actions": ["no-op"]}},
        ]
    }  # fmt: skip
    lines, hidden = lab.drift_lines(plan)
    assert lines == [
        '  update   google_cloud_run_v2_service.svc["web"]: template.max_instance_request_concurrency',
        "  create   google_pubsub_topic.emails[0]",
    ]
    assert hidden == 1


# ---------- checks by name


def test_mail_on_with_both_secrets_mounted_passes(lab):
    def container(env, secrets=None):
        env = [{"name": k, "value": v} for k, v in env.items()]
        env += [{"name": k, "valueSource": {"secretKeyRef": {"secret": s, "version": "latest"}}}
                for k, s in (secrets or {}).items()]  # fmt: skip
        return {"template": {"containers": [{"env": env}]}}

    worker = container({"MAIL_BACKEND": "smtp"}, {"SMTP_PASSWORD": "smtp-password", "MAIL_FROM": "mail-from"})
    reader = "roles/secretmanager.secretAccessor"
    cloud = make_cloud(
        lab,
        gcloud={f"secrets versions list {s} --filter=state=ENABLED": [{"name": "1"}] for s in lab.MAIL_SECRETS},
        api={"services/web": container({"MAIL_BACKEND": "smtp"}), "services/ticket-worker": worker},
        policies={("secret", s): bindings((reader, f"serviceAccount:{sa('ticket-worker')}")) for s in lab.MAIL_SECRETS},
    )
    assert problems(run(lab, lab.check_mail, cloud)) == []
    off = make_cloud(lab, api={"services/web": container({"MAIL_BACKEND": "log"}),
                               "services/ticket-worker": container({"MAIL_BACKEND": "log"})})  # fmt: skip
    assert lab.run_check(off, "mail")[0] == "-"


def test_every_skip_names_parts_terraform_knows(lab):
    for step, parts in lab.SKIPS.items():
        assert set(parts) <= set(lab.PARTS), step
    for name, (parts, _) in lab.SHORTCUTS.items():
        assert name in parts and set(parts) <= set(lab.PARTS)


def test_the_report_lists_every_check_but_the_pause(lab, monkeypatch, tmp_path, capsys):
    lab.write_tfvars(
        {"project_id": P, "github_repo_id": "0", "admin_emails": ["a@example.com"], "lab_parts": ["start"]}
    )
    monkeypatch.setattr(lab, "REPORT", tmp_path / "report.txt")
    monkeypatch.setattr(lab, "cloud_from_settings", lambda: make_cloud(lab))
    monkeypatch.setattr(lab, "own_repo", lambda: None)
    with pytest.raises(SystemExit):
        lab.check(["--report"])
    report = (tmp_path / "report.txt").read_text()
    for name in lab.CHECKS:
        if name != "paused":  # pausing is not a lab task
            assert re.search(rf"^{name} ", report, re.M), name
    assert not re.search(r"^paused ", report, re.M)
    assert set(lab.STEP_CHECKS.values()) <= set(lab.CHECKS)
    assert json.dumps(P)[1:-1] in report


def test_down_deletes_the_state_bucket_and_the_settings_so_up_can_start_over(lab, tmp_path, monkeypatch):
    infra = tmp_path / "infra"
    (infra / ".terraform").mkdir(parents=True)
    monkeypatch.setattr(lab, "INFRA", infra)
    lab.write_tfvars({"project_id": P, "admin_emails": ["a@gmail.com"], "lab_parts": ["start"]})
    lab.link_settings()
    commands = []
    monkeypatch.setattr("builtins.input", lambda prompt: P)
    monkeypatch.setattr(lab, "terraform_env", lambda: {})
    monkeypatch.setattr(lab, "ensure_terraform", lambda env: None)
    monkeypatch.setattr(lab, "adopt", lambda cloud, parts, env: None)
    monkeypatch.setattr(lab, "own_repo", lambda: None)
    monkeypatch.setattr(lab.subprocess, "run", lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, "", ""))
    monkeypatch.setattr(lab, "sh", lambda cmd, **kw: commands.append(cmd) or "")
    lab.down()
    assert ["gcloud", "storage", "rm", "--recursive", f"gs://{P}-tfstate", "--project", P] in commands
    assert not lab.HOME.exists() and not (infra / ".terraform").exists()
    assert not (infra / "terraform.tfvars").is_symlink() and not (infra / "backend.hcl").is_symlink()


def test_down_stops_unless_the_project_id_is_typed(lab, monkeypatch):
    lab.write_tfvars({"project_id": P, "admin_emails": ["a@gmail.com"], "lab_parts": ["start"]})
    monkeypatch.setattr("builtins.input", lambda prompt: "y")
    monkeypatch.setattr(lab, "sh", lambda cmd, **kw: pytest.fail("ran something"))
    with pytest.raises(SystemExit):
        lab.down()
    assert lab.TFVARS.exists()


def test_the_report_does_not_expect_a_paused_scheduler(lab, monkeypatch, capsys):
    lab.write_tfvars({"project_id": P, "admin_emails": ["a@gmail.com"], "lab_parts": ["start"]})
    ran = []
    monkeypatch.setattr(lab, "run_check", lambda cloud, name: ran.append(name) or ("ok", []))
    with pytest.raises(SystemExit):
        lab.check([])
    assert "paused" not in ran and "deploys" in ran
    ran.clear()
    with pytest.raises(SystemExit):
        lab.check(["paused"])
    assert ran == ["paused"]
