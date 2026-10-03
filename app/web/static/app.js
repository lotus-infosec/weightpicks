// WeightPicks bet slip. Alpine CSP build: no inline expressions, so all logic lives
// here and templates reference plain properties and methods only.
"use strict";

(function () {
  function csrf() {
    const meta = document.querySelector('meta[name="csrf-token"]');
    return meta ? meta.content : "";
  }

  // Display only: the server computes the real payout (floor of stake x odds).
  function payoutCents(stakeCents, american) {
    if (!stakeCents || stakeCents <= 0 || !american) return 0;
    const profit = american < 0
      ? Math.floor((stakeCents * 100) / -american)
      : Math.floor((stakeCents * american) / 100);
    return stakeCents + profit;
  }

  function dollars(cents) {
    const sign = cents < 0 ? "−" : "";
    const abs = Math.abs(cents);
    return sign + "$" + Math.floor(abs / 100).toLocaleString("en-US") + "." + String(abs % 100).padStart(2, "0");
  }

  function newKey() {
    if (window.crypto && window.crypto.randomUUID) return window.crypto.randomUUID();
    return String(Date.now()) + Math.random().toString(16).slice(2);
  }

  document.addEventListener("alpine:init", function () {
    window.Alpine.data("slip", function () {
      return {
        open: false,
        busy: false,
        title: "",
        side: "",
        line: "",
        oddsText: "",
        american: 0,
        selectionId: 0,
        oddsVersionId: 0,
        stake: "",
        clientKey: "",
        message: "",
        ok: false,

        get stakeCents() {
          const value = Number.parseFloat(String(this.stake).replace(/[$,\s]/g, ""));
          return Number.isFinite(value) ? Math.round(value * 100) : 0;
        },
        get payoutText() {
          return dollars(payoutCents(this.stakeCents, this.american));
        },
        get canPlace() {
          return !this.busy && this.stakeCents >= 100;
        },
        get cannotPlace() {
          return !this.canPlace;
        },
        get messageClass() {
          return this.ok ? "good" : "bad";
        },
        get sideLabel() {
          return this.side === "over" ? "Over" : "Under";
        },

        pick(event) {
          const d = event.currentTarget.dataset;
          this.title = d.title;
          this.side = d.side;
          this.line = d.line;
          this.oddsText = d.oddsText;
          this.american = Number(d.american);
          this.selectionId = Number(d.selection);
          this.oddsVersionId = Number(d.version);
          this.clientKey = newKey();
          this.message = "";
          this.ok = false;
          this.open = true;
        },
        close() {
          this.open = false;
          this.message = "";
        },
        async place() {
          if (!this.canPlace) return;
          this.busy = true;
          this.message = "";
          try {
            const response = await fetch("/api/bets", {
              method: "POST",
              headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf() },
              body: JSON.stringify({
                selection_id: this.selectionId,
                odds_version_id: this.oddsVersionId,
                stake_cents: this.stakeCents,
                client_key: this.clientKey,
              }),
            });
            const data = await response.json();
            if (data.ok) {
              this.ok = true;
              this.message = "Bet placed: " + dollars(data.stake_cents) + " to return " +
                dollars(data.potential_payout_cents) + ".";
              this.stake = "";
              this.clientKey = newKey();
              document.body.dispatchEvent(new CustomEvent("bet-placed", { bubbles: true }));
            } else {
              this.ok = false;
              this.message = data.message || "That bet couldn't be placed.";
            }
          } catch (err) {
            this.ok = false;
            this.message = "Network problem. Check your connection and try again.";
          } finally {
            this.busy = false;
          }
        },
      };
    });
  });
})();
