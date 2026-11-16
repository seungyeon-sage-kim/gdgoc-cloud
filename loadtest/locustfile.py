"""A signup rush against your own Cloud Ticketing deployment (docs/load-test.md).

Each simulated visitor does what the event page does in a browser (web/static/js/event.js
and ticket.js): it opens the page, checks the event's status every 8 seconds (every 2 while
signups are open), signs up once, then checks its ticket every second until it is issued.

    locust -f loadtest/locustfile.py --headless --host https://YOUR-WEB-ADDRESS \\
        --event-id EVENT_ID --users 300 --spawn-rate 30 --run-time 8m

Every 10 seconds it prints how many web instances answered and how many tickets are
still being issued; at the end, how the signups went. The addresses are @example.com,
which never receive mail. Never point it at a deployment that sends real email.
"""

import itertools
import os
import random
import statistics
import time
import uuid
from collections import Counter
from datetime import datetime

import gevent
from locust import FastHttpUser, events, task

RUN = uuid.uuid4().hex[:6]  # keeps this run's emails apart from an earlier run's
numbers = itertools.count(1)

outcomes = Counter()  # seat, waitlist, full, rate_limited, error
issue_seconds: list[float] = []  # signup → ticket issued: the background work, as a visitor sees it
instances_now: set[str] = set()  # web instances that answered in the current 10 seconds
instances_all: set[str] = set()
visitors: list["Visitor"] = []


@events.init_command_line_parser.add_listener
def _arguments(parser):
    parser.add_argument("--event-id", default="", help="The event to sign up for: the last part of its page address")
    parser.add_argument(
        "--burst",
        action="store_true",
        help="Everyone arrives before the opening and signs up when the countdown ends, within about a second",
    )


@events.request.add_listener
def _served_by(response=None, **_):
    # web/main.py adds "service/revision/instance" to every answer
    who = response.headers.get("X-Served-By") if response is not None and response.headers else None
    if who:
        instances_now.add(who)
        instances_all.add(who)


class Visitor(FastHttpUser):
    def on_start(self):
        self.event_id = self.environment.parsed_options.event_id
        self.state = None  # the event's state: upcoming, open, waitlist, full, closed
        self.opens_in = 0.0  # seconds until signups open, by the server's clock
        self.registration_id = None
        self.signed_up_at = 0.0
        self.ticket = None  # this visitor's ticket status once signed up
        visitors.append(self)
        self.client.get(f"/events/{self.event_id}", name="/events/[id]")

    def wait_time(self):
        """The page's own timers between two requests."""
        if self.registration_id:
            return 1 if self.ticket == "pending" else 5
        return 2 if self.state == "open" else 8

    @task
    def visit(self):
        if self.registration_id:
            self.watch_ticket()
            return
        self.check_status()
        burst = self.environment.parsed_options.burst
        if burst and self.state == "upcoming" and self.opens_in < 8:
            # The page's countdown reaches zero, refreshes the status and enables the button
            gevent.sleep(max(0.0, self.opens_in))
            self.check_status()
        if self.state in ("open", "waitlist"):
            # A person reads the page and fills in the form; in a burst they are already waiting
            gevent.sleep(random.uniform(0.2, 1.0) if burst else random.uniform(1, 8))
            self.sign_up()

    def check_status(self):
        with self.client.get(
            f"/api/events/{self.event_id}/status", name="/api/events/[id]/status", catch_response=True
        ) as answer:
            if answer.status_code == 200:
                data = answer.json()
                self.state = data["state"]
                opens = datetime.fromisoformat(data["opens_at"]) - datetime.fromisoformat(data["server_time"])
                self.opens_in = opens.total_seconds()
            else:
                answer.failure(f"status {answer.status_code}")

    def sign_up(self):
        n = next(numbers)
        # The process id keeps addresses apart when locust runs several processes (--processes)
        body = {"name": f"부하 테스트 {n}", "email": f"load-{RUN}-{os.getpid()}-{n}@example.com", "affiliation": ""}
        with self.client.post(
            f"/api/events/{self.event_id}/registrations",
            json=body,
            name="/api/events/[id]/registrations",
            catch_response=True,
        ) as answer:
            if answer.status_code == 201:
                data = answer.json()
                outcomes["seat" if data["status"] == "pending" else "waitlist"] += 1
                self.registration_id, self.ticket = data["id"], data["status"]
                self.signed_up_at = time.monotonic()
            elif answer.status_code == 409 and answer.json().get("error") == "full":
                outcomes["full"] += 1
                answer.success()  # a correct answer: seats and waitlist are gone
                self.state = "full"
            elif answer.status_code == 429:
                outcomes["rate_limited"] += 1  # the next visit tries again, as a person would
                answer.failure("429: signups_per_minute_per_ip reached on this instance")
            else:
                outcomes["error"] += 1
                answer.failure(f"{answer.status_code}: {answer.text[:120]}")

    def watch_ticket(self):
        with self.client.get(
            f"/api/registrations/{self.registration_id}", name="/api/registrations/[id]", catch_response=True
        ) as answer:
            if answer.status_code != 200:
                answer.failure(f"status {answer.status_code}")
                return
            status = answer.json()["status"]
            if self.ticket == "pending" and status == "issued":
                issue_seconds.append(time.monotonic() - self.signed_up_at)
            self.ticket = status


def _report(environment):
    started = time.monotonic()
    while True:
        gevent.sleep(10)
        issuing = sum(1 for v in visitors if v.ticket == "pending")
        print(
            f"[{time.monotonic() - started:4.0f} s] web instances that answered: {len(instances_now)}"
            f"   tickets still being issued: {issuing}",
            flush=True,
        )
        instances_now.clear()


@events.test_start.add_listener
def _start(environment, **_):
    if not environment.parsed_options.event_id:
        print("--event-id is required: the last part of the event page's address")
        environment.runner.quit()
        return
    environment.reporter = gevent.spawn(_report, environment)


@events.test_stop.add_listener
def _stop(environment, **_):
    if getattr(environment, "reporter", None):
        environment.reporter.kill()
    print("\nSignups:", ", ".join(f"{k} {v}" for k, v in sorted(outcomes.items())) or "none")
    print(f"Different web instances that answered: {len(instances_all)}")
    if issue_seconds:
        print(
            f"Signup to issued ticket: median {statistics.median(issue_seconds):.1f} s,"
            f" slowest {max(issue_seconds):.1f} s ({len(issue_seconds)} tickets)"
        )
