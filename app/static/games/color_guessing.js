const gameKey = document.querySelector(".game-shell").dataset.gameKey;
initGame(gameKey);

window._sideLabel = side => (side === "A" ? "🔴 Red" : "🔵 Blue");

function applyWinner(winner) {
  const ca = el("circle-a"), cb = el("circle-b");
  const pa = el("panel-a"), pb = el("panel-b");
  if (ca) ca.classList.remove("revealing");
  if (cb) cb.classList.remove("revealing");
  if (winner === "A") {
    if (ca) ca.classList.add("winner-glow-red");
    if (pa) pa.classList.add("winner");
    if (pb) pb.classList.add("loser");
  } else if (winner === "B") {
    if (cb) cb.classList.add("winner-glow-blue");
    if (pb) pb.classList.add("winner");
    if (pa) pa.classList.add("loser");
  }
}

function clearBoard() {
  const pa = el("panel-a"), pb = el("panel-b");
  if (pa) pa.className = "group-panel color-panel-red";
  if (pb) pb.className = "group-panel color-panel-blue";
  const ca = el("circle-a"), cb = el("circle-b");
  if (ca) ca.className = "color-circle color-circle-red";
  if (cb) cb.className = "color-circle color-circle-blue";
  clearResult();
}

function renderColorHistory(last10) {
  const track = el("history-track");
  if (!track) return;
  track.innerHTML = "";
  for (let i = 0; i < 10; i++) {
    const badge = document.createElement("div");
    const w = last10[i];
    badge.className = "h-badge" + (w === "A" ? " color-red" : w === "B" ? " color-blue" : "");
    badge.textContent = w === "A" ? "R" : w === "B" ? "B" : "—";
    track.appendChild(badge);
  }
}

function handle(event, data) {
  if (data && data.game_key && data.game_key !== gameKey) return;

  if (event === "server_state") {
    clearBoard();
    if (data.winner) applyWinner(data.winner);
    renderColorHistory(data.last_10_winners || []);
    updateTotals(data);
    const dur = data.phase_duration_seconds;
    const banner = el("banner");
    if (data.phase === "BETTING" && data.phase_ends_at) {
      setBettingOpen(true, "Betting open");
      loadMyBets(window._sideLabel);
      startCountdown("Betting closes in", data, dur || 40);
    } else if (data.phase === "INITIATING" && data.phase_ends_at) {
      setBettingOpen(false, "Betting closed");
      loadMyBets(window._sideLabel);
      startCountdown("Game starts in", data, dur || 10);
    } else if (data.phase === "SETTLING" && data.phase_ends_at) {
      setBettingOpen(false, "Betting closed");
      loadMyBets(window._sideLabel);
      startCountdown("Next round in", data, dur || 10);
    } else {
      setBettingOpen(false, "Waiting for betting window");
      if (banner) banner.textContent = data.in_progress ? "Round in progress…" : "Waiting for next betting window…";
    }
    return;
  }

  if (handlePhaseCommon(event, data, () => loadMyBets(window._sideLabel), clearBoard)) return;

  if (event === "game_started") {
    setBettingOpen(false, "Betting closed");
    stopCountdown();
    clearBoard();
    const ca = el("circle-a"), cb = el("circle-b");
    if (ca) ca.classList.add("revealing");
    if (cb) cb.classList.add("revealing");
    if (el("banner")) el("banner").textContent = "Drawing color…";
    return;
  }

  if (event === "color_revealed") {
    applyWinner(data.winner);
    if (el("banner")) el("banner").textContent = "Color revealed!";
    return;
  }

  if (event === "game_result") {
    const name = data.winner === "A" ? "🔴 Red" : "🔵 Blue";
    showResult(name);
    if (el("banner")) el("banner").textContent = "Round complete.";
    renderColorHistory(data.last_10_winners || []);
    refreshPlayerAmount();
    return;
  }
}

clearBoard();
renderColorHistory([]);
setBettingOpen(false, "Waiting for betting window");
loadMyBets(window._sideLabel);
connectWS(handle);
