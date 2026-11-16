# 사전 학습: 행사 하나의 하루

실습에서 만들 사이트는 행사 신청 사이트입니다. 주최자가 정원과 대기 정원이 있는 행사를 열고, 사람들이 신청 시작 시각에 신청해 QR 티켓을 받습니다. 누가 취소하면 대기 1번이 자리를 받습니다.

목표는 하나입니다. **있는 자리만큼만, 빠짐없이 나눠 준다.** 어려운 점은 둘입니다. 신청은 한순간에 몰리고, 나머지 시간에는 거의 아무도 오지 않습니다.

아래는 행사 하나가 겪는 순간들입니다. 순간마다 무엇이 문제이고 무엇이 필요한지 읽고, 질문에 자기 말로 답해 봅니다. 답을 모르겠으면 링크의 문서를 읽습니다. 실습의 각 과제가 이 순간 중 하나를 다룹니다.

| 순간 | 문제 | 필요한 것 | 실습 과제 |
|---|---|---|---|
| 1. 오픈 순간 | 1초에 수백 명. 서버 한 대는 버티지 못하고, 넉넉히 켜 두면 하루 종일 놀며 돈이 듦 | 몰리면 늘고 없으면 0이 되는 서버: **Cloud Run 자동 확장** | 0, 9 |
| 2. 서버가 늘고 줄 때 | 복사본마다 메모리가 따로이고 언제든 사라짐. 좌석 수를 서버에 두면 틀리거나 잃어버림 | 아무것도 기억하지 않는 서버, 데이터는 밖에: **Firestore, Cloud Storage** | 4, 7 |
| 3. 마지막 한 자리 | 두 사람이 동시에 "1석 남음"을 읽고 둘 다 저장 | 읽고, 확인하고, 저장하는 것을 한 번에: **트랜잭션** | 9 |
| 4. 신청 직후 | QR 만들기와 메일은 느리고 실패할 수 있음. 신청 응답이 기다리면 모두가 기다림 | 일을 메시지로 넘기고 바로 응답: **Pub/Sub, 비동기 처리** | 4 |
| 5. 발급 실패 | 메시지가 실패하거나 두 번 옴 | 간격을 늘려 다시 시도, 두 번 와도 한 번만 처리, 끝내 실패하면 따로 보관: **재시도, 멱등성, dead letter** | 5 |
| 6. 취소 | 자리가 비는 곳이 여러 군데(신청자, 관리자, 마감 처리) | 데이터가 바뀐 것 자체에 반응: **Eventarc, 이벤트 기반** | 6 |
| 7. 확인 마감 | 아무도 클릭하지 않는데 해야 하는 일 | 정해진 시각마다 실행하고 끝나는 일: **Cloud Scheduler, Cloud Run 잡** | 8 |
| 8. 오픈 15분 전 | 0까지 줄어 있으면 첫 신청자가 서버가 뜨기를 기다림(콜드 스타트) | 오픈 시각을 알면 미리 띄워 둠: **최소 인스턴스, 사전 준비** | 8 |
| 9. 부품끼리 부를 때 | 하나가 뚫리면 전부가 뚫림 | 부품마다 신분과 꼭 필요한 권한만: **서비스 계정, 최소 권한, 인그레스, Secret Manager** | 1, 3, 4 |
| 10. 코드를 바꿀 때 | 배포하다 사이트가 멈추거나, 키가 새어 나감 | 자동 검사, 0%로 먼저 띄우고 확인, 키 없는 로그인: **GitHub Actions, Workload Identity Federation, 리비전, 롤백** | 2, 7 |
| 11. 무언가 이상할 때 | 부품이 많아 어디서 잘못됐는지 모름 | 같은 코드가 모든 로그에, 시간에 따른 숫자: **Cloud Logging, Cloud Monitoring** | 4, 5 |

## 답해 볼 질문

1. (순간 1) 인스턴스 하나가 동시에 40개 요청을 처리합니다. 동시 요청이 200개면 인스턴스는 최소 몇 개가 필요한가요? [자동 확장](https://docs.cloud.google.com/run/docs/about-instance-autoscaling?hl=ko), [동시성](https://docs.cloud.google.com/run/docs/about-concurrency?hl=ko)
2. (순간 2) 서비스, 리비전, 인스턴스, 잡은 각각 무엇인가요? [Cloud Run 리소스 모델](https://docs.cloud.google.com/run/docs/resource-model?hl=ko)
3. (순간 3) 두 사람이 마지막 자리를 동시에 신청하면 왜 문제가 되고, 트랜잭션은 어떻게 막나요? [Firestore 트랜잭션](https://docs.cloud.google.com/firestore/docs/manage-data/transactions?hl=ko)
4. (순간 4) 주제와 구독의 차이는? 가져오기(pull)와 푸시(push) 구독은 어떻게 다른가요? [Pub/Sub 개요](https://docs.cloud.google.com/pubsub/docs/overview?hl=ko), [구독 유형](https://docs.cloud.google.com/pubsub/docs/subscription-overview?hl=ko)
5. (순간 6) 이벤트가 생기면 누가 어디로 전달하나요? [Eventarc 개요](https://docs.cloud.google.com/eventarc/docs/overview?hl=ko)
6. (순간 9) 사람 계정과 서비스 계정은 어떻게 다른가요? [서비스 계정 개요](https://docs.cloud.google.com/iam/docs/service-account-overview?hl=ko)
7. HTTP 200, 403, 404, 500은 각각 무슨 뜻인가요? [MDN: HTTP 개요](https://developer.mozilla.org/ko/docs/Web/HTTP/Guides/Overview)
8. 이미지와 컨테이너는 어떻게 다른가요? [Docker: What is a container?](https://docs.docker.com/get-started/docker-concepts/the-basics/what-is-a-container/)

## 용어

| 용어 | 뜻 |
|---|---|
| 콜드 스타트 | 실행 중인 인스턴스가 없어, 요청이 새 인스턴스의 시작을 기다리는 것 |
| 사전 준비(pre-warm) | 몰리기 전에 최소 인스턴스 수를 올려 두는 것 |
| 멱등성 | 같은 일을 여러 번 해도 한 번 한 것과 결과가 같은 성질 |
| dead letter | 재시도에 모두 실패한 메시지를 따로 모아 두는 곳 |
| 롤백 | 문제가 생기면 트래픽을 이전 버전으로 되돌리는 것 |
| 인그레스 | 어디서 오는 요청을 받을지 정하는 설정 |

## 세션 전 준비

- [ ] Google Cloud 무료 체험이 활성 상태인지: ☰ → **결제**에서 남은 기간과 크레딧
- [ ] 개인 GitHub 계정에 로그인되는지
- [ ] [console.cloud.google.com](https://console.cloud.google.com)에서 **Cloud Shell 활성화**를 눌러 터미널이 열리는지
- [ ] 위 질문에 답했는지

설치와 반복 작업은 `./lab` 명령이 대신합니다. 인프라를 코드로 만드는 Terraform은 다음 회차에서 다룹니다.
