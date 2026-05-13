async function refreshPlayerAmount() {
  const response = await fetch("/api/me", { headers: { Accept: "application/json" } });
  if (response.status === 401 || response.status === 403) {
    window.location.href = "/";
    return;
  }
  if (!response.ok) return;
  const data = await response.json();
  const balance  = document.getElementById("player-balance");
  const playerId = document.getElementById("player-id");
  if (balance)  balance.textContent  = data.balance;
  if (playerId) playerId.textContent = data.id;
}

const refreshButton = document.getElementById("refresh-balance");
if (refreshButton) {
  refreshButton.addEventListener("click", refreshPlayerAmount);
}
