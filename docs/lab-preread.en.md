# Pre-read: one event's day

The site you build in this lab takes signups for events. An organizer opens an event with a fixed number of seats and a waitlist; people sign up when signups open and get a QR ticket. When someone cancels, the first person waiting gets the seat.

There is one goal: **give out exactly the seats that exist, and never lose one.** There are two difficulties: signups arrive all at once, and the rest of the time almost nobody comes.

Below are the moments one event goes through. For each one, read what goes wrong and what is needed, then answer the question in your own words. If you can't, read the linked page. Every task of the lab covers one of these moments.

| Moment | Problem | What it needs | Lab tasks |
|---|---|---|---|
| 1. Signups open | Hundreds in one second. One server can't take it; enough servers for the peak sit idle and cost money all day | Servers that add copies under load and go to zero without it: **Cloud Run autoscaling** | 0, 9 |
| 2. Copies come and go | Each copy has its own memory and can disappear at any time. A seat count kept in a server is wrong or lost | Servers that remember nothing, data outside: **Firestore, Cloud Storage** | 4, 7 |
| 3. The last seat | Two people read "1 seat left" at the same moment and both save | Read, check and save as one step: **a transaction** | 9 |
| 4. Right after a signup | Making the QR and sending email are slow and can fail. If the reply waits for them, everyone waits | Hand the work over as a message and reply at once: **Pub/Sub, asynchronous processing** | 4 |
| 5. A ticket fails | A message fails, or arrives twice | Retry with growing waits, handle a repeat once, set aside what keeps failing: **retry, idempotency, dead letter** | 5 |
| 6. A cancellation | Seats free up in several places (the person, an admin, the deadline) | React to the data change itself: **Eventarc, event-driven** | 6 |
| 7. The confirmation deadline | Work nobody clicks for | A run at set times that does its work and exits: **Cloud Scheduler, a Cloud Run job** | 8 |
| 8. 15 minutes before opening | Scaled to zero, the first person waits for a server to start (cold start) | Start copies early when the opening time is known: **minimum instances, pre-warming** | 8 |
| 9. Parts calling parts | One broken part opens everything | Each part its own identity and only the permissions it needs: **service accounts, least privilege, ingress, Secret Manager** | 1, 3, 4 |
| 10. Changing the code | A deploy stops the site, or a key leaks | Automatic checks, a new version at 0% first, sign-in without keys: **GitHub Actions, Workload Identity Federation, revisions, rollback** | 2, 7 |
| 11. Something looks wrong | Too many parts to know where | The same code in every log, numbers over time: **Cloud Logging, Cloud Monitoring** | 4, 5 |

## Questions to answer

1. (Moment 1) One instance handles 40 requests at once. With 200 requests at once, how many instances are needed at least? [Autoscaling](https://docs.cloud.google.com/run/docs/about-instance-autoscaling), [concurrency](https://docs.cloud.google.com/run/docs/about-concurrency)
2. (Moment 2) What are a service, a revision, an instance and a job? [Cloud Run resource model](https://docs.cloud.google.com/run/docs/resource-model)
3. (Moment 3) Why is it a problem when two people take the last seat at once, and how does a transaction prevent it? [Firestore transactions](https://docs.cloud.google.com/firestore/docs/manage-data/transactions)
4. (Moment 4) How do a topic and a subscription differ? A pull and a push subscription? [Pub/Sub overview](https://docs.cloud.google.com/pubsub/docs/overview), [subscription types](https://docs.cloud.google.com/pubsub/docs/subscription-overview)
5. (Moment 6) When an event happens, who delivers it, and where? [Eventarc overview](https://docs.cloud.google.com/eventarc/docs/overview)
6. (Moment 9) How does a service account differ from a person's account? [Service accounts](https://docs.cloud.google.com/iam/docs/service-account-overview)
7. What do HTTP 200, 403, 404 and 500 mean? [MDN: HTTP overview](https://developer.mozilla.org/en-US/docs/Web/HTTP/Guides/Overview)
8. How does an image differ from a container? [Docker: What is a container?](https://docs.docker.com/get-started/docker-concepts/the-basics/what-is-a-container/)

## Terms

| Term | Meaning |
|---|---|
| Cold start | No instance is running, so a request waits for a new one to start |
| Pre-warming | Raising the minimum instances before a rush |
| Idempotency | Doing the same work twice gives the same result as doing it once |
| Dead letter | Where messages that failed every retry are kept |
| Rollback | Moving traffic back to the previous version when something breaks |
| Ingress | Which sources a service accepts requests from |

## Before the session

- [ ] Your Google Cloud free trial is active: ☰ → **Billing** shows days and credit left
- [ ] You can sign in to your personal GitHub account
- [ ] On [console.cloud.google.com](https://console.cloud.google.com), **Activate Cloud Shell** opens a terminal
- [ ] You answered the questions above

`./lab` commands do the setup and the repetitive work. Terraform, which builds infrastructure from code, is the next session.
