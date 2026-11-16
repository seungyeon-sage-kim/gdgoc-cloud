# Google Cloud 설정 (한 번만)

Google Cloud 쪽 전부를 `infra/`의 Terraform이 만듭니다. 콘솔에서 하나씩 누르지 않고, 파일에 적힌 대로 만들고 지웁니다.

| 파일 | 만드는 것 |
|---|---|
| `main.tf` | API, 이미지 저장소(Artifact Registry), 역할별 서비스 계정 8개 |
| `data.tf` | Firestore(TTL, 대기 명단 인덱스, `production_extras`일 때 시점 복구와 매일 백업), 버킷 1개, Secret Manager 비밀값 4개(`ticket-secret`, `session-secret`, 비어 있는 `smtp-password`, `mail-from`) |
| `run.tf` | Cloud Run 서비스 3개(web, ticket-worker, waitlist-promoter), 잡 1개(housekeeping) |
| `events.tf` | Pub/Sub 토픽 2개(`registrations`, `emails`)와 push 구독 2개(`ticket-worker`, `mailer`), 공용 `dead-letter` 토픽, Eventarc Firestore 트리거, Cloud Scheduler(5분마다 housekeeping) |
| `monitoring.tf` | 업타임 체크, SLO 2개, 알림 6개, 로그 기반 지표, 대시보드 (`production_extras`일 때) |
| `abuse.tf` | reCAPTCHA Enterprise 키 (`production_extras`일 때) |
| `iam.tf` | 누가 무엇을 할 수 있는지. 가능한 한 프로젝트 전체가 아니라 버킷, 토픽, 비밀값, 서비스 하나에만 |
| `github.tf` | GitHub Actions의 키 없는 로그인(Workload Identity Federation)과 배포 계정 권한, `production_extras`일 때 Terraform 전용 계정 |

설정 순서(로그인, 저장소, Terraform, 첫 배포, 첫 로그인, 메일)는 [안내서 8장](guide.ko.md#8-실행-배포-정리)에 있습니다.

`terraform.tfvars`에는 행사 설정이 없습니다. 행사는 배포 후 사이트의 `/admin`에서 만듭니다. 체크인만 하는 스태프는 `staff_emails`에 넣습니다. 운영진용 추가 구성은 `production_extras = true`입니다.

## 역할 나누기

- **Terraform**: 서비스의 설정(서비스 계정, 동시성, 최대 인스턴스, 환경 변수, 비밀값)과 그 밖의 모든 자원
- **CI**: 이미지만 교체. push 한 번이 기존 서비스의 새 리비전 하나
- **사이트의 관리 화면**: 행사(일시, 정원, 신청 기간, 참석 확인 마감). Terraform이나 배포 없이 바뀝니다.
- **housekeeping 잡**: 신청 오픈 전후 web의 서비스 수준 최소 인스턴스(사전 준비)

그래서 `run.tf`는 이미지 필드와 서비스 수준 최소 인스턴스를 `ignore_changes`로 둡니다. CI가 이미지를 바꾼 것도, 잡이 최소 인스턴스를 바꾼 것도 drift가 아닙니다. CI 배포 뒤 `terraform plan`에 보이는 `revision -> null`은 web의 후보 태그 때문에 `gcloud`가 정한 리비전 이름이며, 적용해도 같은 이미지의 새 리비전이 하나 생길 뿐입니다.

## Terraform을 CI에서 실행 (`production_extras`)

`production_extras = true`이면 `infra/`가 바뀐 push를 CI가 적용합니다. 순서는 이미지 빌드, plan 저장, 삭제 검사, 저장한 plan 적용, 배포입니다. 적용이 실패하면 배포는 실행되지 않습니다. 각 단계의 설명은 [scripts/infra.sh](../scripts/infra.sh)에 있습니다. 실습 배포에는 이 과정이 없고, `terraform apply`를 직접 실행합니다.

- **켜는 방법**: Terraform 전용 계정도 Terraform이 만들므로 처음 한 번은 노트북에서 실행합니다. `terraform apply` 후 `./connect-github.sh`를 실행하면 `TERRAFORM_SA`와 `TFVARS` 변수가 저장되고, 다음 push부터 CI가 적용합니다. 끌 때는 `gh variable delete TERRAFORM_SA`를 실행합니다. 노트북의 gcloud 로그인과 달리 서비스 계정의 요청은 자기 프로젝트의 API를 쓰므로, Cloud Resource Manager API가 켜져 있어야 합니다(`bootstrap-state.sh`가 켭니다).
- **적용 시점**: 마지막으로 성공한 실행 이후 `infra/`가 바뀌었을 때, 또는 `gh workflow run ci.yml --ref main`으로 직접 실행했을 때입니다. 적용이 실패하면 다음 push가 다시 시도합니다. `DEPLOY_ENABLED`를 끄면 배포와 함께 멈춥니다.
- **삭제 검사**: 자원을 삭제하거나 교체하는 plan이면 CI가 멈춥니다. 멈춘 실행에서 plan을 읽어 본 뒤 `gh workflow run ci.yml --ref main -f allow_destroy=true`로 직접 실행하면 그 실행에서만 삭제를 허용합니다. 누가 실행했는지는 Actions 기록에 남습니다. 비공개 저장소에서는 GitHub Free, Pro, Team 플랜의 필수 검토자(required reviewers)를 쓸 수 없어 이 검사로 대신합니다.
- **설정 값**: `terraform.tfvars`는 저장소에 없고, GitHub 변수 `TFVARS`에 복사본이 있습니다. 값을 바꾸면 `./connect-github.sh`와 `gh workflow run ci.yml --ref main`을 실행합니다. 복사본을 갱신하지 않으면 다음 적용이 값을 이전으로 되돌립니다.
- **권한**: 배포 계정은 IAM, 버킷, 토픽, 비밀값을 바꿀 수 없으므로 `tickets-terraform` 계정을 따로 둡니다. 이 계정의 권한은 프로젝트 소유자에 가깝습니다. `projectIamAdmin`으로 자신에게 권한을 더 줄 수 있고, 상태 파일에 있는 `ticket-secret`, `session-secret`도 읽을 수 있습니다. 즉, `main`에 push할 수 있으면 이 프로젝트를 관리할 수 있습니다. 프로젝트 삭제 권한은 없습니다.
- **되돌리기**: Terraform에는 자동 롤백이 없습니다. `git revert` 후 push하면 이전 설정이 적용됩니다. 적용 도중 실패하면 끝난 부분은 남고, 다음 실행이 나머지를 적용합니다. 배포 실패는 코드만 되돌리므로, 새 설정은 실행 중인 이전 버전에서도 동작해야 합니다.

## 알아 둘 것

- **비밀값과 상태 파일**: Terraform이 `ticket-secret`, `session-secret`을 만들기 때문에 값이 상태 파일(`terraform.tfstate`)에도 들어갑니다. 상태 파일은 비공개 버킷에 두고(`versions.tf`의 backend) 저장소에 올리지 않습니다. `smtp-password`, `mail-from`의 값은 사람이 넣으므로 상태 파일에 없습니다.
- **공개 접근**: web은 `allUsers`에 권한을 주는 대신 Cloud Run의 호출 권한 확인(invoker IAM check)을 끄는 방식으로 공개합니다(`run.tf`의 `invoker_iam_disabled`). Google이 권장하는 방식이고, 2024년 5월 이후 만든 조직에 기본으로 걸리는 도메인 제한 공유 정책에서도 동작합니다. 조직 정책 `run.managed.requireInvokerIam`이 켜져 있으면 이 방식도 거부되므로 정책 예외가 필요합니다.
- **정리**: 실습이 끝나면 `./cleanup.sh --unlink-billing`을 꼭 실행합니다. `terraform destroy` 후 남은 유료 자원을 확인하고, 프로젝트의 결제를 해제합니다.
- **키 없는 로그인 조건**: 저장소의 숫자 id(`github_repo_id`)와 `main` 브랜치만 배포 계정과 Terraform 계정을 쓸 수 있습니다. 이름은 삭제 후 다른 사람이 다시 만들 수 있지만 id는 그렇지 않습니다.
