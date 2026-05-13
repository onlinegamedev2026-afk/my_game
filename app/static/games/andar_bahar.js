const gameKey = document.querySelector(".game-shell").dataset.gameKey;
initGame(gameKey);

let cardsA = [];
let cardsB = [];

window._sideLabel = side => (side === "A" ? "A / Andar" : "B / Bahar");

function renderJoker(card) {
  const box = el("joker-card");
  if (!box) return;
  box.innerHTML = "";
  box.appendChild(card ? makeCard(card.rank, card.suit) : makeSlot());
}

function renderCards(id, cards, winningCard) {
  const row = el(id);
  if (!row) return;
  row.innerHTML = "";
  if (!cards.length) { row.appendChild(makeSlot()); return; }
  cards.forEach(c => {
    const isWin = winningCard && c.rank === winningCard.rank && c.suit === winningCard.suit;
    row.appendChild(makeCard(c.rank, c.suit, isWin));
  });
}

function updateCounts() {
  if (el("count-a")) el("count-a").textContent = `${cardsA.length} cards`;
  if (el("count-b")) el("count-b").textContent = `${cardsB.length} cards`;
}

function clearBoard() {
  cardsA = [];
  cardsB = [];
  const pa = el("panel-a"), pb = el("panel-b");
  if (pa) pa.className = "group-panel";
  if (pb) pb.className = "group-panel";
  clearResult();
  renderJoker(null);
  renderCards("cards-a", []);
  renderCards("cards-b", []);
  updateCounts();
}

function replay(cards, joker, winningCard) {
  clearBoard();
  renderJoker(joker);
  (cards || []).forEach(c => { if (c.group === "A") cardsA.push(c); else cardsB.push(c); });
  renderCards("cards-a", cardsA, winningCard);
  renderCards("cards-b", cardsB, winningCard);
  updateCounts();
}

function handle(event, data) {
  if (data && data.game_key && data.game_key !== gameKey) return;

  if (event === "server_state") {
    replay(data.cards_dealt || [], data.joker || null, data.winning_card || null);
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
    if (el("banner")) el("banner").textContent = "Round started — opening joker…";
    return;
  }
  if (event === "joker_opened") {
    renderJoker(data.joker);
    if (el("banner")) el("banner").textContent = "Joker revealed. Dealing cards…";
    return;
  }
  if (event === "card_dealt") {
    if (data.group === "A") cardsA.push(data); else cardsB.push(data);
    renderCards("cards-a", cardsA);
    renderCards("cards-b", cardsB);
    updateCounts();
    if (el("banner")) el("banner").textContent = `Draw ${data.draw_num} of ${data.total_draws}: ${data.group === "A" ? "Andar" : "Bahar"}`;
    return;
  }
  if (event === "game_result") {
    renderCards("cards-a", cardsA, data.winning_card);
    renderCards("cards-b", cardsB, data.winning_card);
    const name = data.winner === "A" ? "A / Andar" : "B / Bahar";
    showResult(name);
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
