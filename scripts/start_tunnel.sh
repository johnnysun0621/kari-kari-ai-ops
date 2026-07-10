#!/bin/bash
# 老師確認連結對外通道（開發期：Cloudflare 免費隨機網址）
# 前置：brew install cloudflared
# 正式上線改用自有網域的 named tunnel（見 OPERATIONS_PLAYBOOK）。
PORT="${WEB_PORT:-8787}"
echo "🚇 啟動 Cloudflare quick tunnel → http://localhost:${PORT}"
echo ""
echo "等下方出現 https://xxxx.trycloudflare.com 後："
echo "  1. 把該網址填入 .env：TEACHER_LINK_BASE_URL=https://xxxx.trycloudflare.com"
echo "  2. 重啟 bot（連結生成會用新網址）"
echo "  3. 注意：這個免費網址每次重啟 tunnel 都會變"
echo ""
exec cloudflared tunnel --url "http://localhost:${PORT}"
