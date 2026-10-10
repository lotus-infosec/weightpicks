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

  const CAP_MULTIPLE = 100; // parlay payouts are capped at 100x the stake

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

  // After a tab swap, keep the active tab in view in the sideways-scrolling tab row.
  document.addEventListener("htmx:afterSwap", function () {
    const active = document.querySelector(".tabs .tab.active");
    if (active) active.scrollIntoView({ block: "nearest", inline: "nearest" });
  });

  const CHIP_CENTS = [500, 1000, 2500];
  const REDUCED_MOTION = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  function parseSides(text) {
    try {
      const list = JSON.parse(text || "[]");
      return Array.isArray(list) ? list : [];
    } catch (err) {
      return [];
    }
  }

  // The bet sheet (issue #43): tap a market -> pick a side -> stake -> place, or add it to
  // a parlay that waits in a slim bar above the bottom navigation.
  document.addEventListener("alpine:init", function () {
    window.Alpine.data("slip", function () {
      return {
        open: false,
        view: "pick", // pick | parlay | done
        busy: false,
        market: null,
        sideIndex: -1,
        stake: "",
        clientKey: "",
        message: "",
        ok: false,
        doneTitle: "",
        doneText: "",
        parlayAllowed: false,
        maxLegs: 6,
        maxBetCents: 0,
        balanceCents: 0,
        legs: [],

        init() {
          const d = this.$el.dataset;
          this.parlayAllowed = d.parlays === "on";
          this.maxLegs = Number(d.maxLegs) || 6;
          this.maxBetCents = Number(d.maxBet) || 0;
          this.balanceCents = Number(d.balance) || 0;
          const bar = this.$el.querySelector(".parlay-bar");
          if (bar && window.ResizeObserver) {
            new window.ResizeObserver(function () {
              document.body.style.setProperty("--bar-h", bar.offsetHeight + "px");
            }).observe(bar);
          }
          const self = this;
          document.addEventListener("keydown", function (e) {
            if (e.key === "Escape" && self.open) self.close();
          });
        },

        // ---- what the templates read --------------------------------------------------
        get viewPick() { return this.open && this.view === "pick"; },
        get viewParlay() { return this.open && this.view === "parlay"; },
        get done() { return this.open && this.view === "done"; },
        get title() { return this.market ? this.market.title : ""; },
        get line() { return this.market ? this.market.line : ""; },
        get hasLine() { return Boolean(this.market && this.market.line); },
        get lockText() { return this.market ? "Locks " + this.market.lock : ""; },
        get blurb() { return this.market ? this.market.blurb : ""; },
        get marketHref() { return this.market ? "/markets/" + this.market.id : "#"; },
        get hasSide() { return this.sideIndex >= 0; },
        get side() { return this.market && this.sideIndex >= 0 ? this.market.sides[this.sideIndex] : null; },
        get sides() {
          const self = this;
          if (!this.market) return [];
          return this.market.sides.map(function (s, i) {
            return {
              selection: s.selection,
              label: s.label,
              oddsText: s.oddsText,
              kind: "toggle-" + s.side,
              disabled: s.odds === null,
              pressed: i === self.sideIndex ? "true" : "false",
              testid: "pick-" + self.market.id + "-" + s.side,
            };
          });
        },
        get chips() {
          const list = CHIP_CENTS.map(function (c) { return { label: "$" + c / 100, cents: c }; });
          const max = Math.min(this.maxBetCents || Infinity, this.balanceCents || Infinity);
          if (Number.isFinite(max) && max > 0) list.push({ label: "Max", cents: max });
          return list;
        },
        get stakeCents() {
          const value = Number.parseFloat(String(this.stake).replace(/[$,\s]/g, ""));
          return Number.isFinite(value) ? Math.round(value * 100) : 0;
        },
        get combinedDecimal() {
          return this.legs.reduce(function (acc, leg) { return acc * decimal(leg.american); }, 1);
        },
        get payoutCentsValue() {
          if (this.view === "parlay") {
            if (this.stakeCents <= 0 || this.legs.length < 2) return 0;
            return Math.floor(this.stakeCents * this.combinedDecimal + 1e-9);
          }
          return this.side ? payoutCents(this.stakeCents, this.side.odds) : 0;
        },
        get payoutText() { return dollars(this.payoutCentsValue); },
        get overCap() {
          return this.view === "parlay" && this.payoutCentsValue > CAP_MULTIPLE * this.stakeCents;
        },
        get canPlace() {
          if (this.busy || this.stakeCents < 100) return false;
          return this.view === "parlay" ? this.legs.length >= 2 && !this.overCap : Boolean(this.side);
        },
        get cannotPlace() { return !this.canPlace; },
        get placeLabel() {
          if (this.overCap) return "Over the 100x payout cap";
          if (this.busy) return "Placing…";
          if (this.view === "parlay") return "Place parlay · " + this.payoutText;
          return this.stakeCents >= 100 ? "Place bet · returns " + this.payoutText : "Place bet";
        },
        get messageClass() { return this.ok ? "good" : "bad"; },
        get legCountText() {
          return this.legs.length + (this.legs.length === 1 ? " leg" : " legs") +
            (this.legs.length < 2 ? " (add at least 2)" : "");
        },
        get combinedText() { return this.legs.length ? americanText(this.combinedDecimal) : "-"; },
        get barVisible() { return this.legs.length > 0 && !this.open; },
        get barText() {
          return this.legs.length < 2 ? "add one more leg" : "combined " + this.combinedText;
        },
        get inParlay() {
          const id = this.market && this.market.id;
          return this.legs.find(function (l) { return l.marketId === id; }) || null;
        },
        get legLabel() {
          const leg = this.inParlay;
          if (!leg) return "Add to parlay";
          return this.side && leg.selectionId === this.side.selection ? "Remove from parlay" : "Switch parlay leg";
        },

        // ---- actions ----------------------------------------------------------------
        openMarket(event) {
          const d = event.currentTarget.dataset;
          this.market = {
            id: Number(d.market),
            title: d.title,
            metric: d.metric,
            line: d.line,
            version: Number(d.version),
            keys: keysOf(d.keys),
            lock: d.lock,
            blurb: d.blurb,
            sides: parseSides(d.sides),
          };
          const leg = this.inParlay;
          this.sideIndex = leg
            ? this.market.sides.findIndex(function (s) { return s.selection === leg.selectionId; })
            : -1;
          this.view = "pick";
          this.message = "";
          this.ok = false;
          this.clientKey = newKey();
          this.open = true;
        },
        chooseSide(event) {
          this.sideIndex = Number(event.currentTarget.dataset.index);
          this.message = "";
        },
        setStake(event) {
          this.stake = (Number(event.currentTarget.dataset.cents) / 100).toFixed(2);
        },
        openParlay() {
          this.view = "parlay";
          this.message = "";
          this.ok = false;
          this.clientKey = newKey();
          this.open = true;
        },
        close() {
          this.open = false;
          this.message = "";
        },
        toggleLeg() {
          const s = this.side;
          if (!s) return;
          const leg = {
            title: this.market.title,
            pick: s.label + (this.market.line ? " " + this.market.line : "") + " " + s.oddsText,
            american: Number(s.odds),
            selectionId: s.selection,
            oddsVersionId: this.market.version,
            marketId: this.market.id,
            keys: this.market.keys,
          };
          const at = this.legs.findIndex(function (l) { return l.marketId === leg.marketId; });
          if (at >= 0) {
            // The same market: the other side switches the leg, the same side removes it (#37).
            if (this.legs[at].selectionId === leg.selectionId) this.legs.splice(at, 1);
            else this.legs.splice(at, 1, leg);
            this.close();
            return;
          }
          const clash = this.legs.some(function (l) {
            return l.keys.some(function (k) { return leg.keys.indexOf(k) >= 0; });
          });
          if (clash) {
            this.ok = false;
            this.message = "That pick depends on the same weigh-in or day as another leg.";
            return;
          }
          if (this.legs.length >= this.maxLegs) {
            this.ok = false;
            this.message = "A parlay has at most " + this.maxLegs + " legs.";
            return;
          }
          this.legs.push(leg);
          this.close();
        },
        removeLeg(event) {
          const id = Number(event.currentTarget.dataset.selection);
          this.legs = this.legs.filter(function (l) { return l.selectionId !== id; });
          if (!this.legs.length) this.close();
        },
        celebrate(data, parlay) {
          this.view = "done";
          this.doneTitle = parlay ? "Parlay placed" : "Bet placed";
          this.doneText = dollars(data.stake_cents) + " to return " + dollars(data.potential_payout_cents);
          const balance = document.getElementById("balance");
          if (balance && typeof data.balance_cents === "number") {
            this.balanceCents = data.balance_cents;
            balance.textContent = dollars(data.balance_cents);
            balance.classList.remove("pulse");
            void balance.offsetWidth; // restart the animation
            balance.classList.add("pulse");
          }
          const self = this;
          window.setTimeout(function () {
            if (self.view === "done") self.close();
          }, REDUCED_MOTION ? 2500 : 1800);
        },
        async place() {
          if (!this.canPlace) return;
          const parlay = this.view === "parlay";
          this.busy = true;
          this.message = "";
          const url = parlay ? "/api/bets/parlay" : "/api/bets";
          const body = parlay
            ? {
                legs: this.legs.map(function (l) {
                  return { selection_id: l.selectionId, odds_version_id: l.oddsVersionId };
                }),
                stake_cents: this.stakeCents,
                client_key: this.clientKey,
              }
            : {
                selection_id: this.side.selection,
                odds_version_id: this.market.version,
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
              this.stake = "";
              this.clientKey = newKey();
              if (parlay) this.legs = [];
              this.celebrate(data, parlay);
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
