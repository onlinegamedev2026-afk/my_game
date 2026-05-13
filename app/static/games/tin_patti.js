const gameKey = document.querySelector(".game-shell").dataset.gameKey;
initGame(gameKey);

let cardsA = [];
let cardsB = [];

window._sideLabel = side => `Side ${side}`;

function renderCards(id, cards) {
  const row = el(id);
  if (!row) return;
  row.innerHTML = "";
  for (let i = 0; i < 3; i++) {
    row.appendChild(cards[i] ? makeCard(cards[i][0], cards[i][1]) : makeSlot());
  }
}

function clearBoard() {
  cardsA = [];
  cardsB = [];
  const pa = el("panel-a"), pb = el("panel-b");
  if (pa) pa.className = "group-panel";
  if (pb) pb.className = "group-panel";
  clearResult();
  renderCards("cards-a", []);
  renderCards("cards-b", []);
}

function replay(cards) {
  clearBoard();
  (cards || []).forEach(c => {
    if (c.group === "A") cardsA.push([c.rank, c.suit]);
    else                 cardsB.push([c.rank, c.suit]);
  });
  renderCards("cards-a", cardsA);
  renderCards("cards-b", cardsB);
}

function handle(event, data) {
  if (data && data.game_key && data.game_key !== gameKey) return;

  if (event === "server_state") {
    replay(data.cards_dealt || []);
    renderHistory(data.last_10_winners || []);
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
    if (el("banner")) el("banner").textContent = "Round started — dealing cards…";
    return;
  }
  if (event === "card_dealt") {
    const card = [data.rank, data.suit];
    if (data.group === "A") cardsA.push(card); else cardsB.push(card);
    renderCards("cards-a", cardsA);
    renderCards("cards-b", cardsB);
    if (el("banner")) el("banner").textContent = `Draw ${data.draw_num} of 6: Side ${data.group}`;
    return;
  }
  if (event === "game_result") {
    showResult(`Side ${data.winner}`);
    const pa = el("panel-a"), pb = el("panel-b");
    if (data.winner === "A") { if (pa) pa.classList.add("winner"); if (pb) pb.classList.add("loser"); }
    else                     { if (pb) pb.classList.add("winner"); if (pa) pa.classList.add("loser"); }
    if (el("banner")) el("banner").textContent = "Round complete.";
    renderHistory(data.last_10_winners || []);
    refreshPlayerAmount();
    return;
  }
}

clearBoard();
renderHistory([]);
setBettingOpen(false, "Waiting for betting window");
loadMyBets(window._sideLabel);
connectWS(handle);
