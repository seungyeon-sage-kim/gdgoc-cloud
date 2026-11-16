# 부하 테스트

신청 오픈 순간에 방문자가 몰리는 상황을 자신의 프로젝트에서 재현합니다. 확인하는 것은 web 인스턴스의 자동 확장, 동시성, 그리고 Pub/Sub와 ticket-worker가 처리하는 백그라운드 작업입니다.

[loadtest/locustfile.py](../loadtest/locustfile.py)의 가상 방문자는 브라우저의 행사 페이지와 같은 순서로 요청합니다. 페이지를 열고, 8초마다(신청 기간에는 2초마다) 행사 상태를 조회하고, 신청 한 번 후에는 티켓이 발급될 때까지 1초마다 티켓 상태를 조회합니다. 신청 주소는 메일을 받지 않는 `@example.com`입니다.

**실제 메일을 보내는 배포에는 실행하지 않습니다.** 자신의 실습 프로젝트(`mail_enabled = false`)에서만 실행합니다.

## 1. 설정

`infra/terraform.tfvars`에 다음 두 줄을 넣고 `terraform apply`를 실행합니다. [실습](lab.ko.md)에서는 `./lab up`이 같은 값을 넣어 두므로 따로 할 일이 없습니다.

```hcl
signups_per_minute_per_ip = 5000   # 모든 신청이 Cloud Shell 주소 하나에서 나가므로
worker_delay_ms           = 2000   # 메일 서비스 호출에 걸리는 시간을 대신함
```

- 신청 제한은 주소 하나당 1분에 100건이며, 인스턴스마다 따로 계산됩니다. 그대로 두면 신청 대부분이 429로 거부됩니다.
- 메일을 보내지 않는 실습 프로젝트에서는 ticket-worker의 처리가 짧아 대기열이 거의 생기지 않습니다. 2초를 더하면 처리 속도가 초당 약 5건(동시성 2 × 최대 인스턴스 5 ÷ 2초)으로 제한되어, 대기열이 몇 분 동안 남습니다.

## 2. 행사 준비

`/admin`에서 행사를 만들고 공개합니다. 좌석과 대기 인원을 정하고, 신청 오픈 시각은 테스트를 시작할 시각으로 둡니다. 행사 ID는 행사 페이지 주소의 마지막 부분입니다.

오픈 15분 전부터 오픈 60분 뒤까지는 housekeeping 잡이 web의 최소 인스턴스를 3으로 올립니다([cold-start.md](cold-start.md)).

**자동 확장은 사전 준비를 끄고 측정합니다.** 사전 준비를 켜면 인스턴스 3개가 미리 떠 있고, 방문자 300명은 이 3개로 처리되어 인스턴스 수가 늘지 않습니다. 그래서 `prewarm = { instances = 0, minutes_before = 15, minutes_after = 60 }`로 끄고 한 번, 그 줄을 지우고 한 번, 모두 두 번 실행해 비교합니다. 값을 바꾼 뒤에는 `gcloud run jobs execute housekeeping --region asia-northeast3 --wait`로 잡을 바로 실행합니다.

## 3. 실행

Cloud Shell에서는 먼저 `./scripts/setup-cloudshell.sh`로 locust를 설치합니다.

```bash
locust -f loadtest/locustfile.py --headless \
  --host "$(terraform -chdir=infra output -raw web_url)" \
  --event-id 행사_ID --users 300 --spawn-rate 10 --run-time 6m
```

- `--users 300`: 행사 한 번의 규모입니다.
- `--spawn-rate 10`: 방문자가 30초에 걸쳐 도착합니다. 30으로 올리면 Cloud Shell 쪽 DNS 조회가 밀려 페이지 응답이 수십 초로 보이고 실패가 섞입니다. 이때 Cloud Run 로그의 응답 시간은 정상이므로 서버 문제가 아닙니다.
- `--run-time 6m`: Cloud Run과 Pub/Sub의 측정값은 1분 간격으로 수집되므로 짧은 테스트는 그래프에 거의 남지 않습니다.

## 4. 결과 읽기

10초마다 다음 두 값이 출력됩니다.

- `web instances that answered`: 최근 10초 동안 응답한 web 인스턴스 수입니다. 응답 헤더 `X-Served-By`의 인스턴스 ID로 셉니다.
- `tickets still being issued`: 신청은 끝났지만 아직 티켓이 발급되지 않은 수입니다. Pub/Sub에 쌓인 대기열을 방문자 쪽에서 본 값입니다.

끝나면 신청 결과(`seat` 좌석, `waitlist` 대기, `full` 마감, `rate_limited` 429), 응답한 web 인스턴스 수, 신청부터 티켓 발급까지 걸린 시간이 출력됩니다. `seat`는 좌석 수보다 많을 수 없습니다.

콘솔에서는 테스트가 끝나고 3~5분 뒤에 확인합니다. 측정값이 1분 간격으로 수집되고 최대 2분 늦게 표시되기 때문입니다.

- **Cloud Run → web, ticket-worker → 측정항목**: 인스턴스 수, 요청 지연 시간
- **Pub/Sub → 구독 → ticket-worker → 측정항목**: 확인되지 않은 메시지 수, 가장 오래된 메시지의 대기 시간

## 5. 되돌리기

`terraform.tfvars`에서 1단계의 두 줄을 지우고 `terraform apply`를 실행합니다.
