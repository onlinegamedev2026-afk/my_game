/**
 * Shared game logic for all game pages.
 * Individual game files extend this by calling initGame(config).
 */

const SUIT_SYMBOL = { H: "♥", D: "♦", C: "♣", S: "♠" };
const RED_SUITS   = new Set(["H", "D"]);

let _gameKey      = null;
let _bettingOpen  = false;
let _countdownTimer = null;
let _timerDuration  = 0;
let _timerStart     = 0;

// ── DOM helpers ─────────��─────────────────────────────────────���─────────────
function el(id) { return document.getElementById(id); }

function setConnStatus(live) {
  const dot = el("conn-dot");
  const txt = el("conn-status");
  if (dot) dot.className = "conn-dot" + (live ? " live" : "");
  if (txt) txt.textContent = live ? "Live" : "Offline";
}

// ── Bet form ─────────────────────���───────────────────────────────────────────
function setBettingOpen(open, message) {
  _bettingOpen = open;
  const form = el("bet-form");
  if (!form) return;
  form.querySelectorAll("input, button, select").forEach(c => { c.disabled = !open; });
  setBetStatus(message || (open ? "Betting open" : "Betting closed"), open);
}

function setBetStatus(message, ok = false) {
  const s = el("bet-status");
  if (!s) return;
  s.textContent = message;
  s.className = "bet-status" + (ok ? " ok" : (message && !ok ? " error" : ""));
}

// ── Timer / Banner ──────────────────────────────���───────────────────���────────
function startCountdown(label, data, maxSeconds) {
  stopCountdown();
  const initial = Math.min(maxSeconds, Math.max(0, Math.ceil(Number(data.remaining_seconds ?? data.seconds ?? maxSeconds))));
  _timerDuration = initial;
  _timerStart    = performance.now();

  const barWrap = el("timer-bar-wrap");
  const bar     = el("timer-bar");
  if (barWrap) barWrap.hidden = false;

  const render = () => {
    const elapsed   = (performance.now() - _timerStart) / 1000;
    const remaining = Math.max(0, initial - Math.floor(elapsed));
    if (el("banner")) el("banner").textContent = `${label}: ${remaining}s`;
    if (bar) bar.style.width = `${(remaining / initial) * 100}%`;
    if (remaining <= 0) stopCountdown();
  };
  render();
  _countdownTimer = setInterval(render, 250);
}

function stopCountdown() {
  if (_countdownTimer) { clearInterval(_countdownTimer); _countdownTimer = null; }
  const barWrap = el("timer-bar-wrap");
  if (barWrap) barWrap.hidden = true;
}

// ── Betting totals ───────────────────────────────────────────────────────────
function updateTotals(data) {
  const totalA = el("total-a");
  const totalB = el("total-b");
  const box    = el("betting-totals");
  if (!totalA || !totalB) return;
  totalA.textContent = data.group_a_total || "0.000";
  totalB.textContent = data.group_b_total || "0.000";
  if (box) box.hidden = data.hide || !("group_a_total" in data && "group_b_total" in data);
}

// ── My bets ─────────────────────────────���───────────────────────────────��────
function renderMyBets(bets, sideLabel) {
  const list = el("my-bets-list");
  if (!list) return;
  list.innerHTML = "";
  if (!bets || !bets.length) {
    const p = document.createElement("p");
    p.className = "muted";
    p.textContent = "No bets placed yet.";
    list.appendChild(p);
    return;
  }
  bets.forEach(bet => {
    const row    = document.createElement("div");
    row.className = "bet-row";
    const side   = document.createElement("span");
    side.className = "bet-side";
    side.textContent = sideLabel(bet.side);
    const amount = document.createElement("span");
    amount.className = "bet-amount";
    amount.textContent = `${bet.amount} units`;
    const badge  = document.createElement("span");
    badge.className = `bet-status-badge ${bet.status.toLowerCase()}`;
    badge.textContent = bet.status;
    row.append(side, amount, badge);
    list.appendChild(row);
  });
}

async function loadMyBets(sideLabel) {
  const response = await fetch(`/api/games/${_gameKey}/my-bets`, { headers: { Accept: "application/json" } });
  if (!response.ok) return;
  const data = await response.json();
  renderMyBets(data.bets || [], sideLabel);
}

// ── Card rendering ───────────────────────────────────────────────────────────
function makeCard(rank, suit, isWinning = false) {
  const node  = document.createElement("div");
  const cls   = ["playing-card", RED_SUITS.has(suit) ? "red" : "black"];
  if (isWinning) cls.push("winning");
  node.className = cls.join(" ");
  node.textContent = `${rank === "T" ? "10" : rank}${SUIT_SYMBOL[suit]}`;
  return node;
}

function makeSlot() {
  const d = document.createElement("div");
  d.className = "card-slot";
  return d;
}

// ── History ─────────────────────────────────────────────────���────────────────
function renderHistory(last10) {
  const track = el("history-track");
  if (!track) return;
  track.innerHTML = "";
  for (let i = 0; i < 10; i++) {
    const badge = document.createElement("div");
    const w = last10[i];
    badge.className = "h-badge" + (w === "A" ? " side-a" : w === "B" ? " side-b" : "");
    badge.textContent = w || "—";
    track.appendChild(badge);
  }
}

// ── Result panel ─────────────────────────────────────────────────────────────
function showResult(winnerLabel) {
  const r = el("result");
  if (!r) return;
  r.className = "result-panel winner-result";
  const now = new Date().toLocaleTimeString();
  r.innerHTML = `<div class="winner-celebration">
    <span class="winner-cup">🏆</span>
    <span class="winner-headline">Winner &mdash; ${winnerLabel}</span>
    <span class="winner-time">${now}</span>
  </div>`;
}

function clearResult() {
  const r = el("result");
  if (!r) return;
  r.className = "result-panel";
  r.textContent = "";
}

// ── Bet form submission ────────────────────────────��─────────────────────────
function attachBetForm() {
  const form = el("bet-form");
  if (!form) return;
  form.addEventListener("submit", async event => {
    event.preventDefault();
    if (!_bettingOpen) { setBetStatus("Betting is not open right now."); return; }
    const amount = el("bet-amount").value.trim();
    if (Number(amount) < 10) { setBetStatus("Minimum bet is 10.000."); return; }
    setBetStatus("Placing bet…", true);
    const body = new URLSearchParams(new FormData(form));
    const response = await fetch(form.action, {
      method: "POST",
      headers: { "Content-Type": "application/x-www-form-urlencoded", Accept: "application/json" },
      body,
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok || !data.ok) { setBetStatus(data.error || "Bet could not be placed."); return; }
    setBetStatus(data.message || "Bet placed.", true);
    if (data.bets) renderMyBets(data.bets, window._sideLabel || (s => `Side ${s}`));
    refreshPlayerAmount();
  });
}

// ── WebSocket ───────────────────────────────────────────────────────��────────
function connectWS(onMessage) {
  const protocol = location.protocol === "https:" ? "wss" : "ws";
  const socket   = new WebSocket(`${protocol}://${location.host}/ws/games/${_gameKey}`);
  let backoff = 1200;
  socket.onopen    = () => { setConnStatus(true); backoff = 1200; };
  socket.onclose   = () => {
    setConnStatus(false);
    setTimeout(() => connectWS(onMessage), backoff);
    backoff = Math.min(backoff * 1.5, 15000);
  };
  socket.onmessage = msg => {
    const payload = JSON.parse(msg.data);
    onMessage(payload.event, payload.data);
  };
}

// ── Phase helpers (shared state machine) ────────────────────────────────────
function handlePhaseCommon(event, data, loadBets, clearBoard) {
  if (data && data.game_key && data.game_key !== _gameKey) return false;

  if (event === "betting_opened") {
    clearBoard();
    const box = el("betting-totals");
    if (box) { box.hidden = true; updateTotals({ hide: true, group_a_total: "0.000", group_b_total: "0.000" }); }
    setBettingOpen(true, "Betting open");
    renderMyBets([], window._sideLabel || (s => `Side ${s}`));
    startCountdown("Betting closes in", data, data.seconds || 40);
    return true;
  }
  if (event === "game_initiating") {
    setBettingOpen(false, "Betting closed");
    loadBets();
    startCountdown("Game starts in", data, data.seconds || 10);
    return true;
  }
  if (event === "betting_totals") {
    updateTotals(data);
    return true;
  }
  if (event === "settlement_cooldown") {
    setBettingOpen(false, "Betting closed");
    loadBets();
    startCountdown("Next round in", data, data.seconds || 10);
    refreshPlayerAmount();
    return true;
  }
  if (event === "cycle_complete") {
    const box = el("betting-totals");
    if (box) box.hidden = true;
    setBettingOpen(false, "Waiting for betting window");
    if (el("banner")) el("banner").textContent = "Waiting for next betting window…";
    return true;
  }
  if (event === "game_error") {
    if (el("banner")) el("banner").textContent = `Error: ${data.message || "unknown"}`;
    return true;
  }
  return false;
}

// ── Init ─────────────────────────���──────────────────────────────���────────────
function initGame(gameKey) {
  _gameKey = gameKey;
  attachBetForm();
}
