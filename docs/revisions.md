# 리비전, 트래픽 분할, 롤백

**리비전**은 배포 한 번의 고정된 스냅샷입니다: 이미지 + 설정(환경 변수, 동시성, 비밀값 버전, 서비스 계정).
이미지를 바꾸든 환경 변수 하나를 바꾸든 새 리비전이 생기고, 예전 리비전은 그대로 남습니다.
**트래픽**은 어느 리비전이 요청을 몇 % 받는지입니다. 리비전과 트래픽이 분리되어 있어서 배포와 공개를 나눌 수 있습니다.

```bash
gcloud run revisions list --service web --region asia-northeast3          # 리비전 목록
gcloud run services describe web --region asia-northeast3 \
  --format 'table(status.traffic[].revisionName, status.traffic[].percent, status.traffic[].tag)'
```

응답 헤더 `X-Served-By`에 `서비스/리비전/인스턴스`가 찍히므로 어느 리비전이 답했는지 바로 보입니다.

```bash
scripts/latency.sh https://web-…run.app/api/events/<행사 ID>/status 10
```

## CI가 하는 일: 배포와 공개 분리

`.github/workflows/ci.yml`의 deploy 단계는 [scripts/deploy.sh](../scripts/deploy.sh)를 다음 순서로 실행합니다.

1. 지금 트래픽을 받는 리비전과 잡의 이미지를 기록
2. `--no-traffic`: ticket-worker, waitlist-promoter, web의 새 리비전 생성, 트래픽 0%. web에는 `--tag candidate`로 후보 전용 URL(`https://candidate---web-….run.app`) 부여
3. 후보 URL에 smoke test (`/health`, Firestore에서 행사 목록을 읽는 홈 `/`)
4. 통과 시 전환: worker들 `update-traffic --to-latest`, 잡 이미지 교체 후 한 번 실행, 마지막으로 web `update-traffic --to-latest`
5. 실제 URL 확인

어느 단계든 실패하면 모든 서비스와 잡을 1단계에서 기록한 상태로 되돌립니다. 3단계까지는 사용자와 메시지가 새 코드에 닿지 않으므로 바뀌는 것이 없습니다.
실패한 리비전은 트래픽 없이 남아 있어 web의 경우 후보 URL로 살펴볼 수 있습니다. 이때 가장 최신 리비전은 실패한 리비전이므로 `--to-latest`를 실행하면 안 됩니다. 수정한 커밋을 push하면 다음 배포가 트래픽을 옮깁니다.

이 검사들은 각 서비스가 시작되는지, web이 Firestore를 읽을 수 있는지, 잡이 끝까지 실행되는지를 확인합니다. 시작은 하지만 메시지를 잘못 처리하는 worker처럼 테스트가 놓친 버그는 배포될 수 있으며, 이때는 아래 실습 2처럼 되돌립니다.

## 실습 1. 카나리: 10%만 새 버전으로

눈에 보이는 변경으로 새 리비전을 만듭니다. 화면 하단에 표시되는 사이트 이름(`SITE_NAME`)을 바꾸고, 트래픽은 주지 않습니다.

```bash
gcloud run services update web --region asia-northeast3 \
  --update-env-vars SITE_NAME="카나리 리비전" --no-traffic --tag canary
```

- 후보 URL(`https://canary---web-….run.app`)에서는 하단에 새 이름, 원래 URL에서는 예전 이름
- 10%만 보내기:

```bash
gcloud run services update-traffic web --region asia-northeast3 --to-tags canary=10
scripts/latency.sh https://web-…run.app/api/events/<행사 ID>/status 30    # 30번 중 약 3번이 canary 리비전
```

- 문제 없으면 전부: `--to-tags canary=100`, 문제 있으면 되돌리기: `--to-tags canary=0`

## 실습 2. 롤백: 트래픽을 예전 리비전으로

```bash
gcloud run revisions list --service web --region asia-northeast3       # 예전 리비전 이름 확인
gcloud run services update-traffic web --region asia-northeast3 --to-revisions web-00003-lcd=100
```

몇 초 안에 끝납니다. 빌드도 배포도 없이, 이미 있는 리비전으로 요청 방향만 바꾸기 때문입니다. 다만 예전 리비전에는 실행 중인 인스턴스가 없을 수 있으므로 첫 요청은 콜드 스타트를 거칠 수 있습니다.
다시 최신으로: `gcloud run services update-traffic web --region asia-northeast3 --to-latest`

### 롤백과 이전 이미지 재배포의 차이

예전 이미지를 다시 배포하면 *현재* 설정으로 새 리비전이 생깁니다.
설정 변경(환경 변수, 비밀값)이 문제의 원인이었다면 그대로 남습니다. 트래픽 롤백은 이미지와 리비전에 들어 있는 설정(환경 변수, 비밀값, 동시성, 메모리)을 함께 되돌립니다. 최소, 최대 인스턴스와 인그레스, 호출 권한은 리비전이 아니라 서비스에 있는 설정이므로 롤백해도 바뀌지 않습니다.

## 실습 3. 망가진 배포가 사용자에게 닿지 않는 것 확인

테스트는 통과하고 Cloud Run에서만 실패하는 변경을 main에 push합니다. 그냥 홈 화면을 망가뜨리면 테스트에서 먼저 걸려 배포 단계까지 가지 않습니다. `web/events.py`의 `home` 함수 맨 위에 다음 두 줄을 넣습니다. `on_cloud_run`은 Cloud Run에서만 참입니다.

```python
    if get_settings().on_cloud_run:  # only on Cloud Run, so the tests still pass
        raise RuntimeError("deliberate failure")
```

- CI: 후보 smoke test 실패, 전환 단계 건너뜀, `Put everything back` 단계 실행
- 원래 URL: 계속 정상 (이전 리비전)
- ticket-worker, waitlist-promoter, housekeeping 잡: 이전 버전 유지
- 되돌리는 커밋을 push하면 다음 실행에서 정상 배포

## 정리

```bash
gcloud run services update-traffic web --region asia-northeast3 --to-latest --remove-tags canary
cd infra && terraform apply     # gcloud로 바꾼 환경 변수를 Terraform 설정으로 되돌림
```

gcloud로 바꾼 설정은 Terraform 입장에서 drift입니다. 다음 `terraform apply`가 원래 값으로 되돌립니다.
