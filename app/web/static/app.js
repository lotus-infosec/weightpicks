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

  const SIDE_LABELS = { over: "Over", under: "Under", yes: "Yes", no: "No" };
  const CAP_MULTIPLE = 100; // parlay payouts are capped at 100x the stake (D-041)

  function decimal(american) {
    return american < 0 ? 1 + 100 / -american : 1 + american / 100;
  }

  function americanText(d) {
    if (!(d > 1)) return "-";
    const value = d >= 2 ? Math.floor((d - 1) * 100) : -Math.ceil(100 / (d - 1));
    return value > 0 ? "+" + value : "−" + Math.abs(value);
  }

  function keysOf(text) {
    return text ? text.split(",").filter(Boolean) : [];
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
        parlayAllowed: false,
        maxLegs: 6,
        parlay: false,
        legs: [],
        current: null, // the last single pick, so turning on Parlay keeps it as leg 1

        init() {
          const d = this.$el.dataset;
          this.parlayAllowed = d.parlays === "on";
          this.maxLegs = Number(d.maxLegs) || 6;
        },

        get stakeCents() {
          const value = Number.parseFloat(String(this.stake).replace(/[$,\s]/g, ""));
          return Number.isFinite(value) ? Math.round(value * 100) : 0;
        },
        get combinedDecimal() {
          return this.legs.reduce(function (acc, leg) { return acc * decimal(leg.american); }, 1);
        },
        get payoutCentsValue() {
          if (!this.parlay) return payoutCents(this.stakeCents, this.american);
          if (this.stakeCents <= 0 || this.legs.length < 2) return 0;
          return Math.floor(this.stakeCents * this.combinedDecimal + 1e-9);
        },
        get payoutText() {
          return dollars(this.payoutCentsValue);
        },
        get overCap() {
          return this.parlay && this.payoutCentsValue > CAP_MULTIPLE * this.stakeCents;
        },
        get canPlace() {
          if (this.busy || this.stakeCents < 100) return false;
          return this.parlay ? this.legs.length >= 2 && !this.overCap : this.selectionId > 0;
        },
        get cannotPlace() {
          return !this.canPlace;
        },
        get messageClass() {
          return this.ok ? "good" : "bad";
        },
        get sideLabel() {
          return SIDE_LABELS[this.side] || "";
        },
        get single() {
          return !this.parlay;
        },
        get headline() {
          return this.parlay ? "Parlay" : this.title;
        },
        get legCountText() {
          return this.legs.length + (this.legs.length === 1 ? " leg" : " legs") +
            (this.legs.length < 2 ? " (add at least 2)" : "");
        },
        get combinedText() {
          return this.legs.length ? americanText(this.combinedDecimal) : "-";
        },
        get placeLabel() {
          if (this.overCap) return "Over the 100x payout cap";
          return this.parlay ? "Place parlay" : "Place bet";
        },

        legFrom(d) {
          return {
            title: d.title,
            pick: (SIDE_LABELS[d.side] || "") + (d.line ? " " + d.line : "") + " " + d.oddsText,
            american: Number(d.american),
            selectionId: Number(d.selection),
            oddsVersionId: Number(d.version),
            marketId: Number(d.market),
            keys: keysOf(d.keys),
          };
        },
        addLeg(leg) {
          if (this.legs.some(function (l) { return l.marketId === leg.marketId; })) {
            this.message = "That market is already in the parlay.";
            return;
          }
          const clash = this.legs.some(function (l) {
            return l.keys.some(function (k) { return leg.keys.indexOf(k) >= 0; });
          });
          if (clash) {
            this.message = "That pick depends on the same weigh-in or day as another leg.";
            return;
          }
          if (this.legs.length >= this.maxLegs) {
            this.message = "A parlay has at most " + this.maxLegs + " legs.";
            return;
          }
          this.legs.push(leg);
          this.message = "";
        },
        pick(event) {
          const d = event.currentTarget.dataset;
          this.ok = false;
          this.open = true;
          if (this.parlay) {
            this.addLeg(this.legFrom(d));
            return;
          }
          this.title = d.title;
          this.side = d.side;
          this.line = d.line;
          this.oddsText = d.oddsText;
          this.american = Number(d.american);
          this.selectionId = Number(d.selection);
          this.oddsVersionId = Number(d.version);
          this.current = d;
          this.clientKey = newKey();
          this.message = "";
        },
        toggleParlay() {
          this.message = "";
          this.clientKey = newKey();
          if (this.parlay) {
            this.legs = [];
            if (this.current) this.addLeg(this.legFrom(this.current));
          } else {
            this.legs = [];
          }
        },
        removeLeg(event) {
          const id = Number(event.currentTarget.dataset.selection);
          this.legs = this.legs.filter(function (l) { return l.selectionId !== id; });
        },
        close() {
          this.open = false;
          this.message = "";
        },
        async place() {
          if (!this.canPlace) return;
          this.busy = true;
          this.message = "";
          const url = this.parlay ? "/api/bets/parlay" : "/api/bets";
          const body = this.parlay
            ? {
                legs: this.legs.map(function (l) {
                  return { selection_id: l.selectionId, odds_version_id: l.oddsVersionId };
                }),
                stake_cents: this.stakeCents,
                client_key: this.clientKey,
              }
            : {
                selection_id: this.selectionId,
                odds_version_id: this.oddsVersionId,
                stake_cents: this.stakeCents,
                client_key: this.clientKey,
              };
          try {
            const response = await fetch(url, {
              method: "POST",
              headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf() },
              body: JSON.stringify(body),
            });
            const data = await response.json();
            if (data.ok) {
              this.ok = true;
              this.message = (this.parlay ? "Parlay placed: " : "Bet placed: ") +
                dollars(data.stake_cents) + " to return " + dollars(data.potential_payout_cents) + ".";
              this.stake = "";
              this.clientKey = newKey();
              if (this.parlay) this.legs = [];
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
