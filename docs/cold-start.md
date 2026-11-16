# 콜드 스타트와 최소 인스턴스

요청이 없으면 Cloud Run은 인스턴스를 0개로 줄입니다(scale to zero). 비용은 0이지만, 다음 첫 요청은
**새 인스턴스 시작**을 기다립니다. 이것이 콜드 스타트입니다. **최소 인스턴스**를 1 이상으로 두면
그만큼의 인스턴스가 늘 대기하므로 콜드 스타트가 없는 대신, 쉬는 동안에도 비용이 듭니다.

이 설명은 요청이 정말 없을 때의 동작입니다. `production_extras = true`이면 업타임 체크가 5분마다 web을 호출하므로, 최소 인스턴스가 0이어도 web은 보통 인스턴스 1개를 유지합니다(동아리 배포, 2026-11-04 기준 24시간 동안 5분 구간 288개 모두 1개 이상).
콜드 스타트는 업타임 체크가 없는 기본 구성, 또는 그 인스턴스로 처리할 수 없을 만큼 요청이 늘어 새 인스턴스가 시작될 때 나타납니다.

## 측정 결과 (web, 2026-11-01, asia-northeast3)

16분 넘게 요청 없음 → 같은 요청 5번 연속 (행사 상태 API, `scripts/latency.sh`. 측정 당시 버전의 주소는 `/api/status`)

| 설정 | 첫 요청 | 이후 요청 | 새 인스턴스 시작 |
|---|---|---|---|
| 최소 인스턴스 0 | **5.99초** | 약 0.1초 | 있음 (`Starting new instance … AUTOSCALING`) |
| 최소 인스턴스 1 | **0.65초** | 약 0.1초 | 없음 |

최소 인스턴스 0일 때 첫 요청 6초의 내역 (Cloud Logging 시각 기준)

| 구간 | 시간 |
|---|---|
| 인스턴스 시작 → 앱 준비 완료 (컨테이너 시작, Python 모듈 로딩) | 5.3초 |
| 첫 요청 처리 (Firestore 클라이언트 생성, 첫 조회) | 0.44초 |
| 네트워크, TLS | 약 0.5초 |

노트북에서 앱 전체 모듈 로딩 1.2초 중 약 0.6초가 Google Cloud 라이브러리(Firestore, Pub/Sub, Storage) 로딩입니다. Cloud Run의 vCPU 1개에서는 이 시간이 더 길어집니다.

## 직접 측정하기

```bash
scripts/latency.sh https://web-…run.app/api/events/<행사 ID>/status 5    # 행사 페이지 탭을 닫고 16분 이상 쉰 뒤 실행
gcloud logging read 'resource.labels.service_name="web" AND textPayload:"Starting new instance"' --limit 3
```

행사 ID는 행사 주소 `/events/<행사 ID>`의 마지막 부분입니다.

업타임 체크가 켜진 구성에서는 기본 URL이 16분 동안 쉬지 않습니다. 호출되지 않는 예전 리비전에 태그를 달면 전용 URL이 생기고, 그 URL의 첫 요청이 콜드 스타트입니다.

```bash
gcloud run services update-traffic web --region asia-northeast3 --update-tags cold=<예전 리비전>
scripts/latency.sh https://cold---web-…run.app/api/events/<행사 ID>/status 5
```

측정이 끝나면 태그를 지웁니다. 태그가 있는 동안 예전 코드가 실제 데이터에 연결된 채 공개되어 있기 때문입니다.

```bash
gcloud run services update-traffic web --region asia-northeast3 --remove-tags cold
```

최소 인스턴스 바꾸기: `infra/terraform.tfvars`에 `web_min_instances = 1` 추가 후 `terraform apply`. 5분 안에 housekeeping 잡이 적용합니다.

## 언제 최소 인스턴스를 쓰는가

| 상황 | 선택 |
|---|---|
| 평소, 트래픽 거의 없음 | 0. 첫 사용자만 몇 초 대기 (업타임 체크가 켜져 있으면 이 대기도 거의 없음) |
| 신청 오픈 15분 전부터 오픈 60분 뒤까지 | web 3 (아래 사전 준비). 오픈 순간 첫 사용자들이 6초를 기다리지 않음 |
| ticket-worker, waitlist-promoter | 0. 사람이 기다리지 않는 뒤쪽 작업 |

최소 인스턴스는 쉬는 동안에도 과금됩니다(유휴 요금, 처리 중보다 낮음). 금액은
[Cloud Run 가격](https://cloud.google.com/run/pricing)에서 vCPU 1개, 메모리 512MiB 기준으로 확인합니다.

## 신청 오픈 직전 사전 준비 (pre-warm)

행사마다 신청 오픈 시각이 데이터베이스에 있으므로, 5분마다 실행되는 housekeeping 잡([jobs/housekeeping.py](../jobs/housekeeping.py))이
공개된 행사 중 하나라도 오픈 15분 전부터 오픈 60분 뒤 사이에 있으면 web의 최소 인스턴스를 3으로, 그 밖에는 `web_min_instances`(기본값 0)로 둡니다.
잡이 5분 간격으로 실행되므로 실제로는 오픈 10~15분 전에 올라갑니다. 행사가 여러 개여도 Terraform이나 Scheduler 설정을 바꿀 필요가 없습니다.

- 잡은 Cloud Run Admin API로 web의 **서비스 수준** 최소 인스턴스(`scaling.minInstanceCount`)를 바꿉니다. 새 리비전이 생기지 않고, 값이 이미 같으면 아무것도 하지 않습니다.
- `run.tf`는 이 값(`scaling`의 최소 인스턴스)을 `ignore_changes`로 두므로 drift가 아닙니다. 최대 인스턴스는 같은 서비스 수준에서 Terraform이 관리합니다. 잡의 서비스 계정은 web 하나에 대해서만 읽기와 수정 권한(사용자 지정 역할 `webScaler`)과 web 서비스 계정의 `actAs`를 가집니다.
- 숫자는 Terraform 변수 `prewarm = { instances = 3, minutes_before = 15, minutes_after = 60 }`로 바꿉니다.
- 로컬에서는 바꿀 서비스가 없으므로 `python -m jobs.housekeeping`이 대기시킬 인스턴스 수를 출력만 합니다(`python dev.py --open-in 5`로 오픈 직전 행사를 만들어 확인).

3은 추정치입니다. 신청 기간에는 행사 페이지가 2초마다 상태를 조회하므로, 방문자 약 300명이 페이지를 열어 두면 초당 약 150건이 들어옵니다. 처리 중인 요청은 초당 요청 수 × 요청당 시간(약 0.1초) ≈ 15건으로
동시성 40 이내이지만, vCPU 1개의 Python 프로세스는 CPU가 먼저 한계에 닿습니다. 그래서 CPU 기준 2개에 여유 1개를 더했습니다.
부하 테스트([load-test.md](load-test.md), 2026-11-09, 방문자 300명)에서는 인스턴스 3개가 늘어나지 않고 모두 처리했고, 사전 준비 없이 시작하면 0개에서 3개까지 늘어났습니다. 비용은 행사 하나의 오픈마다 인스턴스 3개가 약 75분 동안 유휴 요금으로 과금되는 정도입니다.
ticket-worker는 0으로 둡니다. Pub/Sub가 몰린 메시지를 쌓아 두고, 그 결과를 기다리는 사람이 없기 때문입니다.


## 사전 준비 권한과 판단

처음에는 잡에서 `actAs`를 빼고 읽기와 수정 권한만 주었습니다. 잡이 침해되더라도 web으로 코드를 배포하지 못하게 하려는 목적이었습니다.
실제 배포에서 이 구성은 실패했습니다. 감사 로그에는 `run.services.update`가 허용된 뒤 다음 오류가 남았습니다.

```
Permission 'iam.serviceaccounts.actAs' denied on service account tickets-web
```

즉 Cloud Run은 최소 인스턴스만 바꾸는 요청에도 `actAs`를 요구하며, 필드 단위 권한은 없습니다. 그런데 `actAs`와 수정 권한이 함께 있으면 web으로 새 리비전을 배포할 수 있습니다.

| 선택지 | 결과 |
|---|---|
| 잡에 `actAs` 부여 (현재) | 자동 사전 준비 유지. 범위는 이 잡과 web 계정으로 한정 |
| Cloud Workflows가 권한 보유 | 잡은 0에서 3 사이의 숫자만 요청 가능. 운영할 서비스가 하나 늘어남 |
| 수동 명령 | 잡에 추가 권한이 없음. 오픈 때마다 사람이 실행해야 함 |

현재 구성을 택한 이유는 다음과 같습니다. 잡에는 공개 주소가 없고, 악성 패키지는 어차피 같은 기반 이미지를 쓰는 web에서도 실행되며, 잡은 이미 Firestore 전체를 읽고 쓸 수 있습니다.
실제 서비스로 운영하면서 잡의 코드가 늘어나면 Workflows로 옮기는 것이 좋습니다.

## 운영 환경에서 트래픽 급증에 대응하는 방법

| 급증의 종류 | 대응 |
|---|---|
| 시각을 아는 급증 (신청 오픈, 공연 예매) | 사전 준비: 직전에 최소 인스턴스를 올리고 끝나면 내림 |
| 매일 반복되는 급증 (출근 시간) | 시간대별 최소 인스턴스 예약 |
| 예측할 수 없는 급증 | 자동 확장, 작은 상시 최소 인스턴스, 큐를 이용한 후속 작업 분리, 시작 시간 단축 |
| 처리 한계를 넘는 급증 | 가상 대기실(virtual waiting room)로 입장 속도 조절 |

## 콜드 스타트 자체를 줄이는 방법

- **startup CPU boost**: 시작하는 동안 CPU 추가 (이 프로젝트는 켜져 있음)
- **가벼운 이미지와 의존성**: 불러오는 모듈이 적을수록 빠름
- **시작 시 무거운 작업 피하기**: 연결, 캐시 준비는 첫 요청 때 또는 필요할 때
