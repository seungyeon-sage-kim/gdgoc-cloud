# Cloud Ticketing lab

You put a finished event-signup service into your own Google Cloud project, then sign up and cancel on it yourself and follow where each request goes through the cloud.

## What gets built

![Architecture](architecture.png)

| Component | What it does |
|---|---|
| `web` (Cloud Run service, public) | Event pages and API. Saves the seat in Firestore, leaves a "make a ticket" message, and answers at once |
| Pub/Sub topic `registrations` and subscription `ticket-worker` | Holds messages and delivers them to the worker. Retries on failure; after 5 failures, moves the message to `dead-letter` |
| `ticket-worker` (service, internal only) | Makes the QR ticket, and sends the emails that arrive on the `emails` topic |
| Eventarc trigger and `waitlist-promoter` (service, internal only) | When a registration changes (a cancel), gives the seat to the first person waiting |
| Cloud Scheduler and `housekeeping` (Cloud Run job) | Every 5 minutes: deadlines, resending stuck tickets, filling free seats, pre-warming before an opening |
| Firestore | Events, registrations, seat counters |
| Secret Manager | The mail key (`smtp-password`) and the app's secret values |
| 8 service accounts | One per component, with only the permissions it needs |
| GitHub Actions (your repository) | Checks on every pull request; on a merge to `main`, image builds and deploy |
| Resend (an email service outside Google Cloud) | Sends the real emails |

Terraform builds the infrastructure once (`./lab up`), and GitHub Actions deploys the app.

## The seven key moments

| | Moment | Task | What you learn |
|---|---|---|---|
| 1 | Connect GitHub to Google Cloud by hand without keys, and watch another branch get refused | 2 | Keyless deploy, the trust condition |
| 2 | Follow one signup, connect the queue to the worker yourself, and fix the 403 | 4 | Asynchronous work, ingress vs IAM |
| 3 | Break the worker and watch retries and the dead letter | 5 | Failures are kept, not lost; idempotency |
| 4 | Cancel and watch the waiting person get the seat and the email | 6 | Event-driven design |
| 5 | A bug the tests stop, a problem the 0% deploy stops, and a revert | 7 | CI and CD, revisions, rollback |
| 6 | Measure a cold start, then watch pre-warm before an opening | 8 | Scale to zero, cold start, cost |
| 7 | 300 visitors at once | 9 | Autoscaling, where throughput stops |

---

# Part 1. Setup

## Task 0. Project, budget, your repository, build it all

**Do.**
1. ☰ → **IAM & Admin → Create a project**. Enter a name and note the **Project ID** under it. **Create**.
2. Select the new project in the project menu at the top.
3. ☰ → **Billing**. If it says no billing account is linked, use **Link a billing account** to link your free trial account.
4. ☰ → **Billing** → left menu **Budgets & caps** → **Create new**.
   - **Definition**: **Alerts only**. Any name. **Next**
   - **Scope**: under **Projects**, tick only your lab project. **Next**
   - **Amount**: target amount `12000`. **Next**
   - **Actions**: defaults (50%, 90%, 100%) → **Finish**
5. Make your own repository. [github.com/joyrla/gdgoc-tickets-lab](https://github.com/joyrla/gdgoc-tickets-lab) → **Use this template** (top right) → **Create a new repository**.
   - **Owner**: your account
   - **Repository name**: `gdgoc-tickets`
   - **Public**
   - **Create repository**
6. Click **Activate Cloud Shell** (>_) at the top right of the Console and run:

   ```bash
   gh auth login --web --scopes workflow
   gh repo clone gdgoc-tickets
   cd gdgoc-tickets
   ./lab up
   ```

   - `gh auth login`: press Enter until an 8-character code appears, then in a new tab open [github.com/login/device](https://github.com/login/device) → type the code → **Continue** → **Authorize github**. When it says `Logged in as`, the next line runs. Ignore the `Failed to copy` warning.
   - `./lab up`: shows the project's name and ID and asks. If it is the project from step 1, type `y`. If not, press Enter, pick the project at the top of the Console and reopen Cloud Shell.

`./lab up` uses Terraform to create 3 services, a job, the topics and subscriptions, an Eventarc trigger, the schedule, 8 service accounts, the secrets and the permissions. The services start on Google's sample image; GitHub Actions deploys the real app for the first time in Task 2. Do Task 1 while you wait.

**If it stops, run `./lab up` again.** It keeps what was built and continues. Cloud Shell shuts down after 40 minutes without input, so press Enter now and then.

**Console setup.** In ☰ → **View all products**, click the ☆ of Pub/Sub, Eventarc, Cloud Scheduler and Logging, and they appear at the top of ☰.

**Check.** Open the site address on the last line: it shows Google's sample page.

**Why.** A budget only sends alerts; it does not stop anything. **Do not click "Upgrade to a full account".**

**Stuck?**

| Message | Fix |
|---|---|
| `No project is selected`, or another project's name | Pick the lab project at the top of the Console and reopen Cloud Shell |
| `no billing account linked` | Step 3 |
| `Could not resolve to a Repository` | Step 5 not done yet, or the name is not `gdgoc-tickets`, or the owner is another account |
| `Google Cloud is not ready yet` | Common in a new project. The command retries by itself a minute later |
| Cloud Shell disconnected | Reopen it and run `cd ~/gdgoc-tickets && ./lab up`. If it asks to unlock, type `y` |

## Task 1. Look around (optional, while `./lab up` runs)

Look at what is being built. Parts appear in the Console as `./lab up` finishes them.

**Do.** Find the answers to these five questions in the Console and write them down.

1. ☰ → **Cloud Run → Services** → `web` → **Security** tab: which service account does `web` run as?
2. ☰ → **Pub/Sub → Topics** → `registrations` → **Permissions** on the right: who may send messages to this topic?
3. ☰ → **Pub/Sub → Subscriptions** → `ticket-worker` → **Details**: what is the delivery type, and where does a message go after how many failures?
4. ☰ → **Cloud Run → Services** → tick `ticket-worker` → **Permissions**: who may call `ticket-worker`? Which subscription uses that account right now? (Hint: the `mailer` subscription in Pub/Sub)
5. ☰ → **Eventarc → Triggers** → `registration-changed`: which changes does it react to?

<details>
<summary>Check your answers</summary>

1. `tickets-web`. Every service runs as its own service account.
2. `tickets-web`, `tickets-waitlist-promoter`, `tickets-housekeeping` (Pub/Sub Publisher). Signups, waitlist promotions and resent stuck tickets all send messages to this topic.
3. **Pull**. After 5 failures a message moves to the `dead-letter` topic. Task 4 switches it to Push.
4. `tickets-pubsub-push` (Cloud Run Invoker). Right now the `mailer` subscription (email) signs as this account to call `ticket-worker`. The `ticket-worker` subscription is still Pull, so it doesn't use it yet.
5. A Firestore document update (`document.v1.updated`), only for documents under `registrations/`. The change goes to `waitlist-promoter`.
</details>

(Optional) `./lab iam` shows every account's permissions and the reason for each in one table.

**Why.** The Console shows one component at a time, so how they connect only shows when you follow it, as in these five questions.

---

# Part 2. Connect

## Task 2. Connect GitHub to Google Cloud, first deploy

**Problem.** To deploy to your project, GitHub Actions must sign in to Google Cloud. A service account key stored in GitHub would let anyone deploy the moment it leaked. So instead of a key you build a trust: "only GitHub Actions runs from this repository, on `main`, count as the deploy account."

**Do. Test before connecting**
1. Your repository → **Actions** → left **Sign-in test** → **Run workflow** → **Run workflow**. There is no trust yet, so it fails.
2. Open the failed run → **sign-in** → expand **What GitHub says about this run**. The number after `repository_id:` is your `REPOSITORY_ID`; write it down. GitHub puts it in the token it sends to Google Cloud on every run.

**Do. Build the trust (Console)**

3. ☰ → **IAM & Admin → Workload Identity Federation** → **Create pool**.
   - **Create an identity pool**: **Name** `github`; the **Pool ID** becomes `github` too. **Continue**
   - **Add a provider to pool**: **Select a provider** → **OpenID Connect (OIDC)**. **Provider name** `gdgoc-tickets` (the **Provider ID** becomes `gdgoc-tickets`). **Issuer (URL)** `https://token.actions.githubusercontent.com`. Leave **Audiences** on **Default audience**. **Continue**
   - **Configure provider attributes**: fill three attribute mappings; make the second and third rows with **Add mapping**.

     | Google | OIDC |
     |---|---|
     | `google.subject` | `assertion.sub` |
     | `attribute.repository_id` | `assertion.repository_id` |
     | `attribute.ref` | `assertion.ref` |

   - **Attribute conditions** → **Add condition** → put this in **Condition CEL**, changing only `REPOSITORY_ID` to the number from step 2 → **Save**

     ```
     assertion.repository_id == 'REPOSITORY_ID' && assertion.ref == 'refs/heads/main'
     ```

4. On the new pool `github` → **Grant access** → **Grant access using service account impersonation**.
   - **Select service account**: `tickets-deployer`
   - **Select principals**: attribute name `repository_id`, attribute value `REPOSITORY_ID`
   - **Save**. If a window offers a configuration file to download, close it without downloading.

**Do. Tell GitHub**

5. Your repository → **Settings** → left **Secrets and variables → Actions** → **Variables** tab → **New repository variable**, three times. They are not secret, so they go in the **Variables** tab, not **Secrets**.

   | Name | Value | Where to find it |
   |---|---|---|
   | `GCP_PROJECT_ID` | `PROJECT_ID` | The project menu at the top of the Console |
   | `WIF_PROVIDER` | `projects/PROJECT_NUMBER/locations/global/workloadIdentityPools/github/providers/gdgoc-tickets` | `PROJECT_NUMBER`: ☰ → **Cloud overview → Dashboard**, **Project info** |
   | `DEPLOYER_SA` | `tickets-deployer@PROJECT_ID.iam.gserviceaccount.com` | ☰ → **IAM & Admin → Service accounts** |

6. Wait two minutes, then run **Sign-in test** again. This time it passes.
7. Try another branch. Your repository's main page → the branch menu at the top left (**main**) → type `try-branch` → **Create branch try-branch from main**. **Actions** → **Sign-in test** → **Run workflow** → **Branch** `try-branch` → **Run workflow**. It fails. Take a screenshot of the failed step's error (**Submit 1**). Then the branch menu → **View all branches** → the trash icon on the `try-branch` row.
8. Turn deploys on. **Variables** tab → **New repository variable**: `DEPLOY_ENABLED` = `true`.

**Do. First deploy (pull request)**

You need Git and VS Code on your computer. On a Mac, run `git --version` in Terminal and an install window appears. On Windows, install Git from [git-scm.com](https://git-scm.com/downloads) with the default options.

9. VS Code → top menu **Terminal → New Terminal**. Set your name and email (once), then get your repository. `YOUR_USERNAME` is your GitHub username (not the numeric `REPOSITORY_ID` from step 2).

   ```bash
   git config --global user.name "Your Name"
   git config --global user.email "the email on your GitHub account"
   git clone https://github.com/YOUR_USERNAME/gdgoc-tickets.git
   ```

10. **File → Open Folder** → `gdgoc-tickets` in your home folder. Open **Terminal → New Terminal** again and make a new branch.

    ```bash
    git switch -c first-deploy
    ```

11. In `web/templates/home.html`, replace line 7, `행사를 찾아 신청하고 QR 티켓으로 입장하세요.`, with any sentence, and save.
12. Commit and push to GitHub. The first push opens a GitHub sign-in: sign in in the browser and approve.

    ```bash
    git commit -am "First deploy"
    git push -u origin first-deploy
    ```

13. Open the `https://github.com/YOUR_USERNAME/gdgoc-tickets/pull/new/first-deploy` link in the output → **Create pull request**. The checks (**Checks**) run below it. Nothing is deployed at this stage.
14. When all checks are green, **Merge pull request** → **Confirm merge**.

**Where to look.** **Actions** → the **CI/CD** run started on `main` → the steps of the `deploy` job. ☰ → **Cloud Run** → `web` → **Revisions** tab.

**Check.** When the deploy ends, every job is green, the site changes from the sample page to the real site, and its home page shows your sentence. `web`'s new revision has 100%. In Cloud Shell: `./lab check github deploys`.

**Why.** For every run GitHub issues a short-lived token that contains the repository ID and the branch. The provider from step 3 checks that the token comes from GitHub (the issuer) and accepts only tokens that match the attribute condition (your repository ID, `main`). The grant from step 4 lets only those tokens act as `tickets-deployer`. With no stored key, there is no key to leak or rotate. The condition uses the ID, not the name, so a repository someone recreates under the same name after a deletion does not pass. On a pull request only the checks run; a deploy happens only after the merge to `main`, so `main` always holds code that passed its checks.

**Stuck?**

| Symptom | Fix |
|---|---|
| The second Sign-in test fails with `rejected by the attribute condition` | The condition's `REPOSITORY_ID` must equal the number from step 2, in single quotes |
| `Permission 'iam.serviceAccounts.getAccessToken' denied` | Still spreading: run it again in two minutes. If it stays, the attribute value in step 4 must be `REPOSITORY_ID` |
| `invalid_target`, or the provider is not found | `WIF_PROVIDER` needs the project **number** (not the ID), spelled exactly |
| The `deploy` job is skipped | `DEPLOY_ENABLED` must be exactly `true` |
| The `emulator` job fails with `Transaction lock timeout` | A brief delay in the test database. **Re-run failed jobs** on the run's page |
| `git` not found | Install Git, then restart VS Code |
| Stuck in the Console steps | In Cloud Shell, `./lab github` builds the same trust for you and prints the variable values |

## Task 3. Connect email

Email is off for now: every email the app would send only shows as `not sent` in the logs. You use the **mail key** and **sender address** your instructor sent in a private message. The key is like a password: never put it in chat, code or a screenshot.

**Do.**
1. ☰ → **Security → Secret Manager** → `smtp-password` → **Versions** tab → **New version** → paste the mail key as the value → add it.
2. `mail-from` → **New version** → the sender address → add it.
3. Turn email on.

   ```bash
   ./lab mail on
   ```

4. **In the app.** The site → **로그인** (Sign in) at the top → **이메일** (Email): the address of the Google account you use in the Console → **코드 받기** (Get code).

**Email.** `[Cloud Ticketing] 인증 코드 123456` (sign-in code). Type the code into **인증 코드** (Code) and click **확인** (Confirm). **관리** (Admin) appears in the top menu: `./lab up` registered that account as an organizer.

**Where to look.** `smtp-password` → **Permissions** tab. ☰ → **Cloud Run** → `ticket-worker` and `web` → **Containers** tab → **Variables & Secrets**.

**Check.** Only `tickets-ticket-worker` can read the key, and `web` has no mail key attached. `./lab check mail`.

**Why.** A third-party key goes in Secret Manager, not in code or a plain variable. To change the key you add a new version, and the service reads it when it starts. Even the sign-in email is not sent by `web`: `web` leaves a message on the `emails` topic, the `mailer` subscription delivers it to `ticket-worker`, and the worker sends it through Resend. Only that worker may read the key, so a break-in to `web` would not leak it.

---

# Part 3. Walkthrough

**In the app** lists the buttons to press on the site; **Email** is what should then arrive in your inbox. Every subject starts with `[Cloud Ticketing]`.

## Task 4. Follow one signup, connect the queue

**In the app.**
1. **관리** (Admin) at the top → **새 행사 만들기** (New event).
   - **행사 이름** (Event name): `연결 확인`
   - **주소** (Address): `wiring`
   - **장소** (Venue): `온라인`
   - Leave the dates and seats as they are
   - Tick **공개** (Published) at the bottom → **행사 만들기** (Create event)
2. **행사 페이지** (Event page) → **신청하기** (Sign up). Fill in **이름** (Name) if it is empty, tick **개인정보 수집 및 이용에 동의합니다** (I agree to the privacy terms) → **신청하기**. The email is your signed-in Gmail.
3. **티켓 보기** (View ticket).

**Email.** None.

**Where to look.**
- ☰ → **Firestore** → `(default)` → **Firestore Studio** → `registrations`: your new document and its `status`
- ☰ → **Pub/Sub → Subscriptions** → `ticket-worker` → **Messages** tab → **Pull**. Leave **Enable ack messages** unticked. Pull only once (each pull counts as a delivery attempt).

**Check.** The ticket stays at **티켓 발급 중** (Issuing your ticket), and no email arrives. One `{"registration_id": ...}` message is waiting in the subscription. `web` saved the seat, left the message and answered at once, but the subscription is **Pull**, so the message is kept until someone takes it.

**Do. Connect the queue to the worker (by hand)**
1. ☰ → **Cloud Run → Services** → copy the **URL** of `ticket-worker`.
2. ☰ → **Pub/Sub → Subscriptions** → `ticket-worker` → **Edit**.
3. **Delivery type**: **Push**. **Endpoint URL**: the URL you copied. Leave **Enable authentication** **unticked** → **Update**.

**Where to look.** ☰ → **Cloud Run** → `ticket-worker` → **Observability** tab, logs.

**Check.** `403` and `The request was not authenticated` repeat. Still no email. When you see it 2 or 3 times, take a screenshot of the log (**Submit 2**) and fix it right away. After 5 failures the message moves to `dead-letter`.
1. Subscription → **Edit** → tick **Enable authentication** → **Service account** `tickets-pubsub-push` → **Update**.
2. Wait for your first signup. Do not sign up again.

**Email.** Soon after the fix: `신청 완료: 연결 확인` (signup complete). Its link opens the ticket, now with a QR code.

**Why.** Signup and ticketing are split by a message, so signups keep working even when ticketing is slow or stopped. The confirmation email is sent by the worker that issues the ticket, so there is no email until the message reaches the worker. `ticket-worker`'s ingress is **Internal**, so requests from Pub/Sub may come in (from the internet they get 404). But it requires authentication, so an unsigned request gets 403. With authentication on, Pub/Sub signs as `tickets-pubsub-push`, which holds **Cloud Run Invoker** on `ticket-worker`. Getting through the door (ingress) and being allowed in (IAM) are separate settings.

> **Submit 2 screenshot:** the `403` log (before the fix), the ticket with its QR code (after)

**Stuck?** If no email comes and the ticket stays "issuing" after the fix, the message already moved to `dead-letter`. Send it back with the replay in Task 5.

## Task 5. Worker failure and dead letter

**Do.**
1. ☰ → **Cloud Run** → `ticket-worker` → **Containers** tab → **Variables & Secrets** → **Add variable**: `WORKER_FAIL_RATE` = `1`.
2. Top right **Review changes and redeploy** → check the changed line → **Deploy changes**.

**In the app.** In a browser **private window**, open the site → **연결 확인** → **신청하기** → any **이름**, **이메일** `you+b@gmail.com` (Gmail delivers it to your own inbox whatever follows the `+`) → tick the agreement → **신청하기** → **티켓 보기**.

**Email.** None before the fix.

**Where to look.**
- `ticket-worker` logs: `simulated failure` and `500`
- Subscription `ticket-worker` → **Dead lettering** tab: where failed messages go
- Once the `500`s stop, subscription `dead-letter` → **Messages** → **Pull** (**Enable ack messages** unticked)

**Check.** The signup succeeds. `500` repeats a few times and stops, and the message is in `dead-letter`. Take both screenshots before fixing (**Submit 3**).

Now fix it.
1. In the **Containers** tab, delete `WORKER_FAIL_RATE` → **Review changes and redeploy** → **Deploy changes**.
2. Wait a minute (a message you pulled stays hidden for one minute), then send the failed message back to its topic.
   - Subscription `dead-letter` → **Messages** tab → tick **Enable ack messages** → **Pull**. The pulled message is removed from `dead-letter`. Copy its body, `{"registration_id": "..."}`.
   - ☰ → **Pub/Sub → Topics** → `registrations` → **Messages** tab → **Publish message** → paste the body → **Publish**.

**Email.** Seconds after the replay, `신청 완료: 연결 확인` for `you+b@gmail.com`. A few minutes later, look for `skip ... (status issued)` in the logs.

**Why.** Because signup and ticketing are split by a message, signups keep working while the worker is broken. Retries fix short failures; a failure that retries cannot fix stays in `dead-letter` after 5 tries for a person to look at. The `skip` line is the job, which runs every 5 minutes, sending the stuck ticket once more. Even when the same message arrives twice, the worker first checks whether the ticket was already issued, so there is still one ticket and one email (idempotency).

> **Submit 3 screenshot:** the repeated `500` logs and the message waiting in `dead-letter` (both before the fix)

## Task 6. Cancel and events

**Problem.** When someone cancels, the first person waiting should get the seat. Cancels happen in many places: the attendee, an organizer, the deadline job.

**In the app.**
1. Normal window: **관리** → **새 행사 만들기**. **행사 이름** `대기 배정 확인`, **주소** `promo`, **장소** `온라인`, **정원** (Seats) `1`, **대기 정원** (Waitlist) `1`, tick **공개** → **행사 만들기**.
2. Normal window: **행사 페이지** → **신청하기** → tick the agreement → **신청하기** → **티켓 보기**. Keep this tab open.
3. Private window: the site → **대기 배정 확인** → **대기 신청하기** (Join the waitlist) → **이메일** `you+c@gmail.com` → tick the agreement → **대기 신청하기** → **티켓 보기**. It shows **대기 1번** (waitlist #1).
4. On the normal window's ticket page, at the bottom: **신청 취소** (Cancel registration) → "신청을 취소할까요?" (Cancel?) → **OK**.

**Email.**

| When | To | Subject | Sent by |
|---|---|---|---|
| After 2 | `you@gmail.com` | `신청 완료: 대기 배정 확인` (signup complete) | `ticket-worker`, after issuing the ticket |
| After 3 | `you+c@gmail.com` | `대기 신청 완료: 대기 배정 확인` (on the waitlist) | `web` |
| After 4 | `you@gmail.com` | `신청 취소: 대기 배정 확인` (cancelled) | `web` |
| Seconds after 4 | `you+c@gmail.com` | `자리 배정: 대기 배정 확인` (you got a seat) | `ticket-worker`, after issuing the waiting person's ticket |

**Where to look.** The private window's ticket page (reload it), `waitlist-promoter` logs, and the `status` of both documents in Firestore Studio → `registrations`.

**Check.** Within seconds the private window's ticket shows a QR code and the `자리 배정` email arrives.

**Why.** The cancel changes a Firestore document; Eventarc delivers that change to `waitlist-promoter`, which gives the seat to the first person waiting and leaves a message on the `registrations` topic, so the ticket and the `자리 배정` email go out the same way as in Tasks 4 and 5. `web` knows nothing about the waitlist. Because the system reacts to the data change itself, a cancel from anywhere is handled in one place. If delivery fails, the job that runs every 5 minutes fills free seats anyway.

## Task 7. Pull requests, checks, safe deploys

**Problem.** Code that passes the tests can still fail in the real environment. You make two mistakes on purpose and see where each one is stopped.

**Do. A bug the tests stop (CI)**
1. In VS Code's terminal, update `main` and make a new branch.

   ```bash
   git switch main
   git pull
   git switch -c seat-bug
   ```

2. In `shared/models.py`, line 121, change `held < self.seat_limit` to `held <= self.seat_limit`. The bug lets one more person in after the event is full.
3. Commit and push, then open a pull request from the link in the output.

   ```bash
   git commit -am "Seat bug"
   git push -u origin seat-bug
   ```

**Check.** The checks' `test` and `emulator` jobs are red. The red marks only say `Process completed with exit code 1.`, so open the `test` job's log: the first failure is `test_never_oversells_under_concurrency`, with `assert 31 == 30` (31 people for 30 seats). The merge button still works with red checks. Do not merge: **Close pull request**. `main` and the site are unchanged.

**Do. A problem the tests cannot see (CD)**
4. Make a new branch from `main`.

   ```bash
   git switch main
   git switch -c wrong-port
   ```

5. In `Dockerfile`, line 42 (the `web` stage), change `--port ${PORT:-8080}` to `--port 3000`. The container now answers on a port other than the 8080 Cloud Run waits on.
6. Commit, push and open a pull request. All checks are green. **Merge pull request** → **Confirm merge**.

   ```bash
   git commit -am "Wrong port"
   git push -u origin wrong-port
   ```

**Where to look.** **Actions** → the **CI/CD** run on `main` → the `deploy` job. ☰ → **Cloud Run** → `web` → **Revisions** tab.

**Check.** The new revision starts at 0% of traffic and fails. The failed step is **Start the new revisions with no traffic**, with the error `The user-provided container failed the configured startup probe checks`; the revision's own log says `STARTUP HTTP probe failed ... on port 8080 path "/health"`. Cloud Run counts a container as started only when it answers `/health` on port 8080, and this one waited on 3000. Then **Put everything back** runs and traffic stays on the previous revision. The site keeps working. Take a screenshot of that **Revisions** tab and of the failed step (**Submit 4**).

**Do. Undo it**
7. GitHub → the merged pull request `wrong-port` → **Revert** at the bottom → **Create pull request** → when its checks are green, **Merge pull request** → **Confirm merge**.

**Check.** The next deploy starts a new revision at 0%, checks it, then moves it to 100%.

**Why.** CI (the checks on a pull request) stops mistakes in the code before they reach `main`. But some mistakes, like the port, exist only in the real environment. CD (the deploy) starts each new version as a revision with 0% of traffic and moves traffic only after it starts and passes its check. If it fails, traffic stays on the previous revision, and users notice nothing. A revision and its traffic are separate settings, so a rollback only moves traffic, with no build. **Revert** ships a new change that undoes the old one, through the same pipeline.

> **Submit 4 screenshot:** the failed deploy step and the **Revisions** tab at that moment (the failed revision, traffic still 100% on the previous one)

## Task 8. Cold start and pre-warm

**Cold start.** Do this first, when the site has not been opened for more than 15 minutes.

```bash
./lab coldstart
```

**Check.** The first request takes seconds; the next ones under 0.1 s. Write both in the measurement table.

**Pre-warm.**
1. Create an event whose signups open in 10 minutes; pre-warm starts 15 minutes before an opening. The site → **관리** → **새 행사 만들기**: **행사 이름** `오픈 준비 확인`, **주소** `opening`, **장소** `온라인`, **신청 시작** (Signups open) 10 minutes from now, tick **공개** → **행사 만들기**.

2. ☰ → **Cloud Scheduler** → tick the `housekeeping` row → **Force run**, so the job runs now instead of in up to 5 minutes.
3. ☰ → **Cloud Run → Jobs** → `housekeeping` → **History** tab → the run you just started → logs.

**Check.** The log says `pre-warm: web minimum instances 0 → 3`. In `web` → **Observability**, the instance count graph shows 3 instances. An hour after the opening, the job sets it back to 0.

**Why.** With 0 instances, the first request waits for a container to start. Scaling to zero means no cost at night; the price is a slow first request. When the opening time is known, the job starts instances ahead of time. The wait disappears, but idle instances cost money. The job may change `web`'s settings because of a custom role with just two permissions (read and update; see `housekeeping` in `./lab iam`).

> **Submit 5 screenshot:** the `./lab coldstart` output, the graph with 3 instances before the opening

## Task 9. The rush

**Do.**
1. The site → **관리** → **새 행사 만들기**: **행사 이름** `신청 몰림 A`, **주소** `rush-a`, **장소** `온라인`, **신청 시작** 3 minutes from now, **정원** (Seats) `100`, **대기 정원** (Waitlist) `50`, tick **공개** → **행사 만들기**.
2. Start the load test right away. 300 visitors keep the event page open before the opening and press **신청하기** the moment it opens.

   ```bash
   ./lab load rush-a
   ```

**Email.** None. The visitors' addresses are `@example.com`, a domain reserved for testing, so nothing is sent even with email on; the logs say `test address, not sent`.

**Where to look.** (graphs appear 3 to 5 minutes late)
- `web` → **Observability**: instance count, request latency
- Subscription `ticket-worker` → **Metrics**: unacked messages
- Firestore Studio → `event_counts` → `rush-a`: `held` (registrations holding a seat) and `issued` (tickets issued)

**Check.** Locust's `Signups:` line and response-time table, and `held` = `issued` = 100. Write them in the measurement table and take the screenshots (**Submit 6**). Because the event opens within minutes, pre-warm (Task 8) may have started 3 instances already; look at how many more than 3 the graph shows.

**Why.** Cloud Run adds instances by looking at concurrent requests per instance. With lower concurrency, the same load needs more instances. Every signup changes the event's single seat-counter document in a transaction, so seats are exact but signups per second have a ceiling. More instances do not raise it.

> **Submit 6 screenshot:** `web`'s instance count graph, the Locust result (`Signups:` line and response-time table)

---

# Submission

Upload to the announced form. **Required** items are graded; **optional** ones are not.

## Required 1. Six key concepts

One screenshot and one question per concept. Three to five sentences per answer is enough.

**Scoring:** 3 points per concept. Screenshot 1 point, answer 2 points (1 for a correct answer, 2 if the reason is backed by your own screenshot or measurements).

**1. Keyless deploy (Task 2)**
- Screenshot: **Submit 1**. The failed Sign-in test on `try-branch`
- Question: what stops someone else's repository, or another branch, from deploying to your project? Why does GitHub not need to store a key?

**2. Service accounts and IAM (Task 4)**
- Screenshot: **Submit 2**. The `403` in `ticket-worker`'s logs (before the fix), the ticket with its QR code (after)
- Question: which setting let the request in, and which one refused it with 403? Which account, with which role, let it through?

**3. Pub/Sub and asynchronous work (Task 5)**
- Screenshot: **Submit 3**. The repeated `500` logs, the message waiting in `dead-letter`
- Question: while the worker was failing, what did the person signing up experience, and what did they not (the signup page, the ticket, the email)? Where did the message end up, and why there?

**4. CI, CD, revisions (Task 7)**
- Screenshot: **Submit 4**. The failed deploy step and the **Revisions** tab
- Question: where was each of the two mistakes stopped, and why did the second one pass the checks? Which users saw the failed revision?

**5. Cold start, pre-warm, cost (Task 8)**
- Screenshot: **Submit 5**. The `./lab coldstart` output, the graph with 3 instances before the opening
- Question: why was the first request slow? How does pre-warm prevent it, and what does it cost? Name one case where pre-warm is not needed.

**6. Autoscaling and throughput (Task 9)**
- Screenshot: **Submit 6**. The instance count graph, the Locust result
- Question: how many instances did `web` reach, and why? Why did signups per second stop rising even as instances were added?

## Required 2. Also hand in

1. `~/lab-report.txt`. After the tasks, run `./lab check --report` in Cloud Shell to make it.
2. The measurement table

   | | `rush-a` (concurrency 40) | (Optional) `rush-c` (concurrency 2) |
   |---|---|---|
   | Most `web` instances | | |
   | Signup response, median (50%) | | |
   | Signup response, 95% | | |
   | Most signups per second | | |

   Cold start, first request / later requests: ____ / ____.

3. One design question: **if ten times as many people signed up at the opening, what would hit its limit first, and what would you change?** Base it on your own measurements.
4. The feedback form (confidence per concept, 1 to 5, and the most confusing task)

## Optional

| Task | Concept | Question |
|---|---|---|
| 1 | The components | Your five answers. Which connection was hard to see from the Console alone? |
| 3 | Secret Manager | Which account can read the mail key? If the key leaked, what would you do? |
| 4 | Pull subscription | The signup answered, so why were there no ticket and no email? Where was the message? |
| 5 | Idempotency | Why is there one ticket and one email even if the same message arrives twice? |
| 6 | Eventarc | Who gave the seat to the waiting person, and what started it? Of the four emails, why did `web` send some and the worker the others? |
| 8 | Jobs and Cloud Scheduler | Why is `housekeeping` a job and not a service? |

# Further (optional, goals only)

- Measure Task 9 again at concurrency 2 (`rush-c`) and compare instances and response times
- When the event is full, answer "full" without a transaction, and measure Task 9 again
- Split the seat counter across several documents and measure again
- Add one alert that would have caught Task 5's failure
- Find the trust you clicked together in Task 2 in the Terraform code (`infra/`) and write down how to build it in code

# Clean up

When you have submitted and are done with the lab, delete everything it made.

1. Run this in Cloud Shell. It starts once you type the project ID.

   ```bash
   cd ~/gdgoc-tickets
   ./lab down
   ```

2. Your repository → **Settings** → at the bottom, **Delete this repository**.
3. (Optional) To delete the project too: ☰ → **IAM & Admin → Settings** → **Shut down**. It is deleted for good after 30 days.

`./lab down` hands Terraform what you built by hand (the trust from Task 2, the push setting from Task 4), deletes it all with `terraform destroy`, checks that nothing is left, deletes Terraform's state bucket and the Cloud Shell settings, and turns deploys off on GitHub. If it stops partway, run it again.

# Shortcuts

All run from `~/gdgoc-tickets` in Cloud Shell.

| Command | What it does |
|---|---|
| `./lab up` | Build all the infrastructure. Run it again to put the code's settings back |
| `./lab iam` | Every account's permissions and why |
| `./lab github` | (Optional) build Task 2's keyless sign-in for you and print the variable values |
| `./lab mail on`, `./lab mail off` | Turn real email on or off |
| `./lab load EVENT_ID [--users N] [--minutes M]` | Load test |
| `./lab coldstart` | First-request and later-request times |
| `./lab check [NAME]`, `./lab check --report` | Checks and the report |
| `./lab code` | An organizer's sign-in code (while email is off) |
| `./lab down` | Delete everything the lab made |

# Reference. If you know AWS

| AWS | This lab (Google Cloud) |
|---|---|
| Account | Project |
| App Runner, ECS Fargate service | Cloud Run service (`web`, two workers) |
| ECS run-task | Cloud Run job (`housekeeping`) |
| SNS + SQS | Pub/Sub. The topic is like SNS; each subscription is like an SQS queue |
| SQS dead-letter queue | The dead-letter topic and its subscription |
| EventBridge rule | Eventarc trigger |
| EventBridge Scheduler | Cloud Scheduler |
| DynamoDB | Firestore |
| Secrets Manager | Secret Manager |
| IAM role a service assumes | Service account |
| Resource policy (`lambda:InvokeFunction`) | A role granted on the resource (`Cloud Run Invoker`) |
| GitHub OIDC identity provider | Workload Identity Federation |
| CloudFormation stack | Nothing in the Console; the Terraform code and state play that role |
