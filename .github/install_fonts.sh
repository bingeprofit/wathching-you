#!/usr/bin/env bash
# 나눔고딕 설치. apt 미러가 가끔 실패한다 (2026-11-11 22:50·00:20 KST 경제지표, 23:32 미국 수급 — apt exit 100).
# apt 를 재시도하고, 그래도 안 되면 google/fonts(GitHub)에서 같은 파일 이름으로 직접 받는다.
# 스크립트들은 /usr/share/fonts/truetype/nanum/NanumGothic.ttf 를 찾는다. 끝내 실패해도 잡은 멈추지 않는다(한글만 깨짐).
set -u
D=/usr/share/fonts/truetype/nanum
have() { [ -s "$D/NanumGothic.ttf" ]; }
have && exit 0
# 미러가 '응답 없음'으로 걸리면 apt 가 수십 분 매달린다 (2026-11-12 재실행에서 15분+) → 단계마다 timeout 으로 자른다.
A="-o Acquire::Retries=2 -o Acquire::http::Timeout=20 -o Acquire::https::Timeout=20 -o DPkg::Lock::Timeout=60"
timeout 120 sudo apt-get install -y -qq $A fonts-nanum >/dev/null 2>&1 && have && exit 0
timeout 180 sudo apt-get update -qq $A >/dev/null 2>&1 || true
timeout 120 sudo apt-get install -y -qq $A fonts-nanum >/dev/null 2>&1 && have && exit 0
echo "apt fonts-nanum 실패 → google/fonts 에서 직접 받음"
U=https://raw.githubusercontent.com/google/fonts/main/ofl/nanumgothic
sudo mkdir -p "$D"
if sudo curl -fsSL --retry 3 -o "$D/NanumGothic.ttf" "$U/NanumGothic-Regular.ttf" &&
   sudo curl -fsSL --retry 3 -o "$D/NanumGothicBold.ttf" "$U/NanumGothic-Bold.ttf"; then
  sudo fc-cache -f >/dev/null 2>&1 || true
  echo "나눔고딕 직접 설치 완료"
else
  echo "::warning::나눔고딕 설치 실패 — 차트의 한글이 깨질 수 있음"
fi
exit 0
