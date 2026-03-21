# AutoEQ MCP 서버

AI 어시스턴트가 [AutoEQ](https://github.com/jaakkopasanen/AutoEq) 헤드폰 이퀄라이제이션 데이터베이스에 접근할 수 있게 해주는 MCP(Model Context Protocol) 서버입니다. **8,800개 이상의 헤드폰/IEM**에 대한 파라메트릭 EQ 설정, 음향 시그니처 분석, Harman 선호도 점수를 제공합니다.

[English README](README.md)

## 이런 걸 할 수 있습니다

AI 어시스턴트에게 이렇게 물어보세요:

- *"HD650 EQ 설정 알려줘"*
- *"HE400se랑 HD600 비교해줘"*
- *"따뜻한 소리의 오버이어 헤드폰 추천해줘"*
- *"Harman 점수 높은 IEM 순위 보여줘"*
- *"밝은 소리 좋아하는데 인이어 뭐가 좋을까?"*

서버가 8개 주파수 대역별로 측정치를 자동 분석하고, 각 헤드폰의 음향 시그니처(Neutral, Warm, Bright, Dark, V-shaped 등)를 분류합니다.

## 도구

| 도구 | 설명 |
|------|------|
| `eq_search` | 이름, 타입(over-ear/in-ear/earbud), 음향 시그니처, 측정 소스로 검색 |
| `eq_profile` | 상세 EQ 프로필 — 파라메트릭 EQ, 고정밴드 EQ, 대역별 분석(시각화 바 포함) |
| `eq_compare` | 두 헤드폰을 전 주파수 대역에 걸쳐 나란히 비교 |
| `eq_recommend` | 선호도별 추천 (neutral, warm, bright, bass, vocal, fun, analytical) |
| `eq_ranking` | Harman headphone listener preference score 순위 |
| `eq_targets` | 61개 타겟 커브 목록 (Harman, Diffuse Field 등) |
| `eq_sync` | AutoEQ GitHub에서 최신 데이터 가져와서 DB 재구축 |

## 출력 예시

```
# Sennheiser HD 650
- Source: oratory1990
- Type: over-ear
- Harman preference score: 84.0
- Sound signature: Neutral, Harman-like

## Per-band analysis (deviation from target, dB)
  Sub-bass (20-60Hz):   -3.2 dB [·······▓▓▓|··········] sub-bass lacking
  Bass (60-250Hz):      +0.8 dB [··········|··········] close to target
  Mid (500-1kHz):       -0.3 dB [··········|··········] close to target
  Presence (2k-4kHz):   +1.4 dB [··········|▓·········] detail emphasis
  Air (8k-20kHz):       -2.1 dB [········▓▓|··········] closed / lacking air

## Parametric EQ (Preamp: -6.5 dB)
  #  Type        Fc (Hz)      Q  Gain (dB)
  1  LowShelf        105   0.70       +6.5
  2  Peaking        1800   1.20       -2.3
  ...
```

## 설치

### Claude Code / Claude Desktop (stdio)

```bash
# 설치
pip install autoeq-mcp

# 초기 DB 동기화 (AutoEQ 레포 클론 + SQLite DB 구축, ~20초)
autoeq-mcp --sync

# Claude Code에 추가
claude mcp add autoeq_mcp -- autoeq-mcp
```

Claude Desktop의 경우 설정 파일에 추가:

```json
{
  "mcpServers": {
    "autoeq": {
      "command": "autoeq-mcp"
    }
  }
}
```

### SSE 모드 (원격 / 멀티 클라이언트)

```bash
# SSE 서버 시작
AUTOEQ_MCP_PORT=3008 autoeq-mcp --sse

# DNS rebinding 방지용 허용 호스트 설정
AUTOEQ_MCP_ALLOWED_HOSTS="your-domain.com,localhost" autoeq-mcp --sse
```

### 소스에서 설치

```bash
git clone https://github.com/verIdyia/autoeq-mcp
cd autoeq-mcp
pip install -e .
autoeq-mcp --sync
```

## 설정

모든 설정은 환경변수로 합니다:

| 변수 | 기본값 | 설명 |
|------|--------|------|
| `AUTOEQ_DATA_DIR` | `~/.autoeq-mcp` | 레포 클론과 SQLite DB 저장 경로 |
| `AUTOEQ_MCP_PORT` | `3008` | SSE 서버 포트 |
| `AUTOEQ_MCP_HOST` | `0.0.0.0` | SSE 서버 호스트 |
| `AUTOEQ_MCP_ALLOWED_HOSTS` | *(없음)* | SSE용 허용 호스트 (쉼표 구분) |

## 데이터 출처

모든 헤드폰 데이터는 Jaakko Pasanen의 [AutoEQ](https://github.com/jaakkopasanen/AutoEq) (MIT 라이선스)에서 가져옵니다.

- **8,800+** 헤드폰/IEM 프로필
- **22개** 측정 소스 (oratory1990, crinacle, Rtings 등)
- **61개** 타겟 커브 (Harman 2018/2019, Diffuse Field 등)
- **2,300+** Harman 선호도 점수

데이터베이스는 AutoEQ GitHub 레포에서 동기화합니다. `eq_sync` 또는 `autoeq-mcp --sync`로 업데이트하세요.

## 음향 시그니처 분류 방식

서버는 각 헤드폰의 주파수 응답 오차(타겟 대비 편차)를 8개 대역별로 분석하여 분류합니다:

| 시그니처 | 특성 |
|----------|------|
| **Neutral** | 모든 대역이 타겟 ±2 dB 이내 |
| **Warm** | 저음 강조, 고음 평탄~억제 |
| **Bright** | 고음 강조, 저음 평탄~억제 |
| **Dark** | 고음 억제 |
| **V-shaped** | 저음↑ + 고음↑, 중음↓ |
| **U-shaped** | 저음↑ + 고음↑ |
| **Bass-heavy** | 저음 강하게 강조 (>3 dB) |
| **Mid-forward** | 중음 강조, 저음/고음 평탄 |
| **Harman-like** | 평균 총 편차 < 1.5 dB |

## 라이선스

MIT — [LICENSE](LICENSE) 참조
