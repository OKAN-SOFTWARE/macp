# 카카오톡 "나에게 보내기" 연동 설정 (롤백 전용 — 기본 채널은 Slack §8)

> **Slack 단독 전환 완료.** 기본 알림 채널은 Slack(§8)이고 카카오톡은 **롤백 전용 백엔드**입니다.
> Slack 시크릿이 없는 호스트에서는 카톡이 폴백 채널로 동작하므로 이 문서의 1~7번 설정은
> 그런 호스트에서만 필요합니다. 카톡으로 되돌리려면 `NOTIFY_BACKENDS=kakao`(§8-3).

매일 아침 brief가 생성되면 [Todo/Briefings/](../Todo/Briefings/) 의 요약을 알림 채널로 전송합니다
(기본 Slack; Slack 시크릿이 없으면 카카오톡 본인 채팅방으로 폴백).

## 1. Kakao Developers 앱 등록 (5분, 1회)

1. <https://developers.kakao.com/console/app> 접속, 카카오 계정 로그인
2. **앱 추가하기** → 이름 "Obsidian Brief" → 회사명 "Personal"
3. 생성된 앱 → **앱 키** 페이지에서 **REST API 키** 복사 → 환경변수로 저장
   ```sh
   export KAKAO_REST_API_KEY=<복사한 키>
   ```
   (선택) **보안 → Client Secret** 을 "사용함"으로 켰다면 secret 도 추가:
   ```sh
   export KAKAO_CLIENT_SECRET=<발급된 시크릿>
   ```
   helper / refresh / install 스크립트 모두 이 env 가 있으면 자동으로 사용합니다.
4. **카카오 로그인** 메뉴 → **활성화 설정** ON, Redirect URI 등록: `http://localhost:8765`
5. **카카오 로그인 → 동의항목** 메뉴 → **카카오톡 메시지 전송 (talk_message)** 활성화

## 2. OAuth 토큰 발급 (1분, 1회)

```sh
cd ~/Projects/MaestroACP
export KAKAO_REST_API_KEY=<위에서 복사한 키>
bash scripts/kakao_oauth_helper.sh
```

브라우저가 열리면 카카오 로그인 + 동의 → 자동으로 `.secrets/kakao-token.json`에 저장됩니다.

## 3. (선택·기본 비활성) 자동 토큰 갱신 — 롤백 전용

> **Slack 단독 전환(§8) 이후 카톡은 롤백 전용 백엔드이므로 토큰 자동 갱신은 기본 비활성입니다.**
> 평소에는 설치하지 않아도 됩니다. 아래 둘 다 명시 옵트인이 있어야만 켜집니다:
>
> - **리눅스/systemd**: `scripts/systemd/gen_units.py`가 `macp-kakao-refresh` 유닛을
>   디스크에는 쓰지만 `INSTALL_ONLY`라 `--enable`로 켜지지 않습니다. 롤백하려면 사람이 한 줄:
>   `systemctl --user enable --now macp-kakao-refresh.timer`.
> - **맥/LaunchAgent**: 설치기가 기본으로 설치를 거부합니다. 켜려면 옵트인이 필요합니다:
>   ```sh
>   KAKAO_REFRESH_INSTALL=1 KAKAO_REST_API_KEY=<키> bash scripts/install_kakao_refresh_launchagent.sh
>   ```

옵트인해 설치하면 4시간마다(00:30·04:30·08:30·12:30·16:30·20:30) + 로그인 시 `refresh_token`으로 `access_token`을 갱신합니다. (access_token 수명 ~6h)

`refresh_token` 자체가 만료(약 60일)되면 `kakao_oauth_helper.sh`를 다시 실행하세요.

## 4. 동작 확인

```sh
bash scripts/notify_kakao.sh
```

본인 카톡 "나에게 보내기" 채팅방에 오늘 brief 요약이 도착하면 성공.

## 5. brief.sh와 자동 연동

[brief.sh](brief.sh)는 `.secrets/kakao-token.json`이 있으면 brief 생성 직후 자동으로 카톡으로 push 합니다.

토큰 없이 brief.sh를 실행해도 brief는 정상 생성되며, "kakao token not configured — skipping push" 메시지만 출력됩니다.

## 6. 프로젝트별 프로필(별도 앱) 추가하는 법

기본 설정은 앱 하나로 모든 알림을 보내므로, 카톡 목록에서 **어느 프로젝트가 보낸 알림인지
구분되지 않습니다**. 프로젝트마다 Kakao 앱을 따로 등록해 **프로필**로 붙이면, 카드 상단에
그 프로젝트 이름이 발신 라벨로 찍힙니다.

현재 쓰는 프로필 이름: `default`(vault brief·워커) / `cgv` / `kis` / `tennis`.

### 6-1. 새 앱 등록

§1과 동일하되 **앱 이름을 프로젝트 이름으로** 지정합니다 (이 이름이 알림에 보이는 라벨).
Redirect URI는 기존과 같은 `http://localhost:8765`, 동의항목도 `talk_message` 그대로입니다.

### 6-2. 토큰 발급

```sh
cd ~/Projects/MaestroACP
export KAKAO_REST_API_KEY=<새 앱의 REST API 키>
export KAKAO_CLIENT_SECRET=<새 앱의 시크릿>   # 켰다면
export KAKAO_DISPLAY_NAME="CGV 아이맥스"      # 생략하면 프로필명이 라벨
bash scripts/kakao_oauth_helper.sh cgv
```

두 파일이 생깁니다 (둘 다 600, gitignore):

| 파일 | 내용 |
|------|------|
| `.secrets/kakao-token-cgv.json` | access_token / refresh_token |
| `.secrets/kakao-app-cgv.json` | `rest_api_key`, `client_secret`, `display_name` |

> **app 파일은 선택이 아닙니다.** 갱신 스크립트는 named 프로필의 크리덴셜을 **오직 이 파일에서만**
> 읽습니다. 환경변수(`KAKAO_REST_API_KEY` 등)는 LaunchAgent가 export 하는 **default 앱**의 것이라,
> 이걸 named 프로필에 쓰면 client_id 불일치로 카카오가 갱신을 거부하고 그 토큰은 6시간 뒤 조용히
> 죽습니다. 그래서 app 파일이 없는 프로필은 폴백하지 않고 **스킵**합니다.

경로만 확인하고 싶으면 브라우저를 열지 않는 dry-run:

```sh
KAKAO_OAUTH_DRY_RUN=1 KAKAO_REST_API_KEY=x bash scripts/kakao_oauth_helper.sh cgv
```

### 6-3. 보내기

```sh
KAKAO_PROFILE=cgv bash scripts/kakao_send.sh "예매 열림" "https://example.com/booking"
```

- `KAKAO_PROFILE` 미설정 = 기존과 완전히 동일 (default 토큰 + text 템플릿)
- 프로필 지정 시 `feed` 템플릿으로 전환되고 `item_content.profile_text`에 `display_name`이 들어갑니다
- 프로필 **토큰 파일이 없으면** stderr에 경고를 남기고 **default로 폴백해 계속 전송**합니다
  (알림을 잃지 않는 쪽을 택함). 로그에 `falling back to default`가 보이면 6-2를 다시 도세요.

### 6-4. 갱신

`scripts/kakao_token_refresh.sh`는 인자 없이 부르면 `.secrets/kakao-token*.json`을 **전부 순회**하며
각자의 app 파일로 갱신합니다. 4시간마다 도는 LaunchAgent가 그렇게 부르므로 **새 프로필은 자동으로
갱신 대상에 포함됩니다 — 별도 등록 작업이 없습니다.**

```sh
bash scripts/kakao_token_refresh.sh          # 전부
bash scripts/kakao_token_refresh.sh cgv      # 하나만
```

종료코드: 1개라도 갱신되면 0, 전부 실패해야 1 (side 프로젝트 하나가 죽었다고 LaunchAgent 잡 전체를
실패로 표시하지 않기 위함). 따라서 **개별 프로필의 실패는 종료코드가 아니라 로그로 확인**합니다:

```sh
grep kakao_token_refresh logs/notify_kakao.out.log | tail -20
```

`refresh_token`(약 60일) 자체가 만료된 프로필은 6-2를 그 프로필 이름으로 다시 실행하세요.

## 7. 보안 메모

- `.secrets/kakao-token.json`은 600 permission, gitignore 됨 (확인: `git check-ignore .secrets/kakao-token.json`)
- access_token이 노출되면 메시지 전송만 가능 (앱 권한 한정) — 본인 계정 talk_message scope만 영향
- 의심스러우면 Kakao Developers → 앱 → **앱 키 재발급** → `kakao_oauth_helper.sh` 다시 실행

## 8. Slack 알림 백엔드 (기본 채널)

`scripts/kakao_send.sh`는 채널 무관 **디스패처**입니다. Slack 단독 전환 이후 **Slack이 기본
채널**이고 카톡은 롤백 전용입니다(아래 §8-3). 카톡 refresh_token은 주기적으로 만료되고(§3)
자동 갱신도 기본 비활성이므로, 상시 알림은 Slack으로 나갑니다. Slack 백엔드는 stdlib `urllib`만
쓰는 `scripts/slack_send.py`로, 별도 의존성 설치가 없습니다.

### 8-1. 워크스페이스·채널·앱 (1회)

1. Slack 워크스페이스 1개를 만들거나 기존 것을 씁니다.
2. 알림을 받을 **채널**을 만듭니다. macp가 쓰는 채널 키: `default`(워커·브리프) / `kis` /
   `digest`(일일 요약). 프로젝트별로 더 추가해도 됩니다. 각 채널은 분리된 방이라 카톡처럼
   발신처가 섞이지 않습니다.
3. Slack 앱 **1개**를 만들어(`https://api.slack.com/apps` → *Create New App* → *From scratch*)
   **Incoming Webhooks** 기능을 켜고, **채널마다 webhook을 하나씩** 추가합니다
   (*Add New Webhook to Workspace* → 대상 채널 선택). 앱 하나에 채널별 webhook URL이 쌓입니다.
4. 각 채널의 알림 설정을 **"모든 새 메시지(All new messages)"**로 둬야 폰이 울립니다
   (기본값은 멘션만). 채널 이름 → *Notification preferences*.

### 8-2. 시크릿 파일

채널 키 → webhook URL 맵을 `.secrets/notify-slack.json`에 둡니다 (600, gitignore 대상):

```json
{
  "webhooks": {
    "default": "<default 채널의 incoming webhook URL>",
    "kis": "<kis 채널의 incoming webhook URL>",
    "digest": "<digest 채널의 incoming webhook URL>"
  }
}
```

- webhook URL 자체가 **자격증명**입니다(알면 누구나 그 채널에 글을 씀). 이 파일은 커밋 금지,
  문서·코드·로그에 URL 원문을 넣지 마세요. `slack_send.py`는 실패 로그에도 URL을 마스킹합니다.
- 채널 키가 맵에 없으면 `default` webhook으로 폴백하고, `default`도 없으면 그 발송만 건너뜁니다
  (경고만, 카톡 발송에는 영향 없음).
- 이 webhook 방식(이하 **(A)**) 대신 **봇 토큰 방식(B)**도 쓸 수 있습니다(§8-6). 같은 시크릿
  파일에 둘 다 있으면 **(B)가 우선**합니다.

### 8-3. 켜는 법 (`NOTIFY_BACKENDS`) — Slack 단독 전환 완료

- **기본 규칙 (cutover)**: `NOTIFY_BACKENDS` 미설정일 때
  - 시크릿 파일(`.secrets/notify-slack.json`)이 **있으면 `slack` 단독** — 카톡 HTTP는 0회입니다.
    (병행 발송 기간이 끝났고 Slack이 상시 채널입니다.)
  - 시크릿 파일이 **없으면 `kakao` 폴백** — Slack이 없는 호스트는 알림을 잃지 않도록 기존처럼
    카톡으로만 보냅니다.
- **롤백·명시 지정**: `NOTIFY_BACKENDS=kakao`(카톡 단독 롤백) / `NOTIFY_BACKENDS=kakao,slack`(병행) /
  `NOTIFY_BACKENDS=slack`(Slack만) 등으로 콤마 구분해 덮어쓸 수 있습니다. **목록 첫 항목이 primary**이고,
  `KAKAO_SEND_STRICT=1`의 성공 판정(종료코드)은 primary 백엔드 성공을 기준으로 합니다. primary가 아닌
  백엔드의 실패(예: 기본 primary=slack에서 카톡 실패, 또는 병행 중 Slack 실패)는 항상 비치명입니다.
- 채널 키는 `NOTIFY_CHANNEL_KEY`(최우선) → `KAKAO_PROFILE` → `default` 순으로 고릅니다. 일일 요약
  `kakao_digest_send.py`는 `NOTIFY_CHANNEL_KEY=digest`를 자동으로 설정합니다.
- 시크릿도 없고 `NOTIFY_BACKENDS`도 미설정이면 동작은 §1~§7과 **완전히 동일**합니다(카톡 폴백, Slack HTTP 0회).

### 8-4. 동작 확인

```sh
KAKAO_PROFILE=default bash scripts/slack_send.py "테스트" "https://example.com"
# 또는 디스패처를 통째로 (기본은 시크릿 있으면 Slack 단독):
bash scripts/kakao_send.sh "테스트 알림"
# 카톡까지 함께 보고 싶으면 병행을 명시:
NOTIFY_BACKENDS=kakao,slack bash scripts/kakao_send.sh "테스트 알림"
```

`default` 채널에 메시지가 도착하면 성공. 아침 브리프(`scripts/notify_kakao.sh`)도 같은 선택 규칙으로
기본은 `default` 채널에 Slack 단독 발송하며, Slack 시크릿이 없는 호스트는 카톡으로 폴백합니다.

봇 토큰 방식(B)도 **같은** `slack_send.py`/`kakao_send.sh` 호출로 검증됩니다 — 시크릿 파일에
(B) 스키마가 있으면 자동으로 `chat.postMessage` 경로가 쓰이므로 위 명령을 그대로 돌리면 됩니다.

### 8-5. Slack Free 요금제의 한계와 장기 기록

Slack Free는 **최근 90일 메시지만** 보관하고, 활동이 없으면 앱/워크스페이스가 비활성화될 수 있습니다
(대략 1년). 따라서 Slack은 **휘발성 실시간 알림 채널**로만 쓰고, 보존이 필요한 기록은 Slack에
의존하지 마세요 — macp의 장기 기록은 vault의 `Memory/`(세션·에피소드)와 `logs/`(발송 로그
`logs/kakao_send.out.log` 등)에 남습니다. 요약 큐 자체도 `logs/kakao_digest.jsonl` /
`logs/kakao_digest.sent.jsonl`로 보존됩니다(§KAKAO_ALERTS.md).

### 8-6. (B) 봇 토큰 방식 (`chat.postMessage`) — webhook 대안

incoming-webhook(A)은 채널마다 URL을 따로 만들어야 하고 URL 자체가 영구 자격증명입니다. 대신
**봇 사용자 1개 + 채널 ID 맵**으로 보내려면 (B)를 씁니다. 봇은 `chat.postMessage` API로 글을 쓰고,
채널은 webhook URL이 아니라 **채널 ID**로 지정합니다.

1. **앱 scope 설정** (`https://api.slack.com/apps` → 앱 선택 → *OAuth & Permissions* →
   *Bot Token Scopes*, 또는 *App Manifest*에서 bot scopes로):
   - `chat:write` — 메시지 전송(필수)
   - `channels:manage` · `channels:join` — 봇이 공개 채널에 스스로 들어가기
   - `channels:read` — 채널 목록·ID 조회
2. **설치**: *Install to Workspace*(이미 설치돼 있으면 *Reinstall*)로 scope를 반영하고,
   발급된 **Bot User OAuth Token**(`xoxb-…`)을 복사합니다.
3. **봇 초대**: 대상 채널마다 `/invite @<앱 이름>`으로 봇을 넣습니다(비공개 채널은 반드시 초대 필요).
4. **채널 ID 확인**: 채널 이름 → *View channel details* 맨 아래의 채널 ID(`C…`)를 복사합니다
   (채널 **이름**이 아니라 ID를 씁니다).
5. **시크릿 파일**: `.secrets/notify-slack.json`에 (B) 스키마로 둡니다(600, gitignore 대상):

   ```json
   {
     "bot_token": "xoxb-…",
     "channels": {
       "default": "<default 채널 ID>",
       "kis": "<kis 채널 ID>",
       "digest": "<digest 채널 ID>"
     }
   }
   ```

- 채널 키 해석(`NOTIFY_CHANNEL_KEY` → `KAKAO_PROFILE` → `default`)과 `default` 폴백 규칙은
  (A)와 **동일**합니다. 키가 맵에 없고 `default`도 없으면 그 발송만 건너뜁니다(경고만).
- **봇 토큰도 자격증명**이고 채널 ID도 민감 정보입니다. 이 파일은 커밋 금지, 문서·코드·로그에
  원문을 넣지 마세요. `slack_send.py`는 실패 로그에 토큰·채널 ID를 넣지 않고
  `profile=<키> http=<코드> ok=false error=<slack 에러>` 형태로만 마스킹해 남깁니다.
- 같은 파일에 `webhooks`(A)와 `bot_token`(B)이 모두 있으면 **(B)가 우선**합니다. (A)로 되돌리려면
  `bot_token`을 지우면 됩니다.
- 전송 실패 판정: Slack은 HTTP 200에도 `{"ok": false, "error": …}`로 실패를 알리므로, 코드는
  응답의 `ok`를 검사합니다(예: `not_in_channel` = 봇이 그 채널에 없음 → §8-6 3번 초대 확인).
