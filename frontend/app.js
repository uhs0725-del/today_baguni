"use strict";

(function () {
  var WEEKDAYS = ["일", "월", "화", "수", "목", "금", "토"];

  var els = {
    date: document.getElementById("today-date"),
    sampleBadge: document.getElementById("sample-badge"),
    loading: document.getElementById("loading-state"),
    error: document.getElementById("error-state"),
    cards: document.getElementById("cards"),
    filters: document.getElementById("filters"),
    sorts: document.getElementById("sorts"),
    recmenu: document.getElementById("recmenu"),
    recmenuChips: document.getElementById("recmenu-chips"),
    recmenuGo: document.getElementById("recmenu-go"),
    feedEmpty: document.getElementById("feed-empty"),
    retry: document.getElementById("retry-btn"),
    actionbar: document.getElementById("actionbar"),
    actionbarCount: document.getElementById("actionbar-count"),
    actionbarClear: document.getElementById("actionbar-clear"),
    actionbarGo: document.getElementById("actionbar-go"),
    comboSheet: document.getElementById("combo-sheet"),
    comboBackdrop: document.getElementById("combo-backdrop"),
    comboClose: document.getElementById("combo-close"),
    comboBody: document.getElementById("combo-body"),
  };

  // Selected ingredient names (server resolves combo recipes by name).
  var selected = [];

  // Full fetched+ranked list (order preserved) and the active filter group.
  // "전체" shows a ranked shortlist (decision-reduction); a category chip
  // shows that whole category. Filtering is client-side (no re-fetch).
  var allItems = [];
  var activeGroup = "전체";
  var OVERVIEW_LIMIT = 15;

  // Sort applied inside the card list. Default mirrors the header sub-copy
  // ("평소보다 싸고"): cheapest-vs-usual first. Independent of the filter chip.
  var activeSort = "discount";

  // The 3 "평소보다 가장 싸진" names backing the recommended-menu block.
  // Computed globally from allItems (NOT affected by the active filter).
  var top3Names = [];

  function formatDate(isoDate) {
    // isoDate "YYYY-MM-DD" from server (already KST). Build local Date safely.
    var parts = (isoDate || "").split("-");
    var d;
    if (parts.length === 3) {
      d = new Date(
        parseInt(parts[0], 10),
        parseInt(parts[1], 10) - 1,
        parseInt(parts[2], 10)
      );
    } else {
      d = new Date();
    }
    return (
      d.getMonth() + 1 + "월 " + d.getDate() + "일 " + WEEKDAYS[d.getDay()] + "요일"
    );
  }

  function fmtPrice(n) {
    return (n || 0).toLocaleString("ko-KR") + "원";
  }

  function pill(changePct) {
    var span = document.createElement("span");
    span.className = "pill";
    if (changePct === null || changePct === undefined) {
      span.className += " pill--flat";
      span.textContent = "변동 정보 없음";
    } else if (changePct <= -0.5) {
      span.className += " pill--down";
      span.textContent = "▼ " + Math.abs(Math.round(changePct)) + "%";
    } else if (changePct >= 0.5) {
      span.className += " pill--up";
      span.textContent = "▲ " + Math.round(changePct) + "%";
    } else {
      span.className += " pill--flat";
      span.textContent = "변동 없음";
    }
    return span;
  }

  function tag(text, modifier) {
    var s = document.createElement("span");
    s.className = "tag" + (modifier ? " " + modifier : "");
    s.textContent = text;
    return s;
  }

  function buildCard(item) {
    var li = document.createElement("li");
    li.className = "card";

    // ----- head -----
    var head = document.createElement("div");
    head.className = "card__head";

    var main = document.createElement("div");
    main.className = "card__main";

    var name = document.createElement("div");
    name.className = "card__name";
    name.textContent = item.name;

    var price = document.createElement("div");
    price.className = "card__price";
    price.textContent = fmtPrice(item.price) + " / " + item.unit;
    var priceTag = document.createElement("span");
    priceTag.className = "price-tag";
    priceTag.textContent = "기준가";
    price.appendChild(priceTag);

    var badges = document.createElement("div");
    badges.className = "card__badges";
    badges.appendChild(tag(item.category));
    if (item.solo_fit >= 4) badges.appendChild(tag("1인 적합", "tag--solo"));
    if (item.season) badges.appendChild(tag("제철", "tag--season"));

    main.appendChild(name);
    main.appendChild(price);
    main.appendChild(badges);

    if (item.storage_tip) {
      var tip = document.createElement("div");
      tip.className = "card__tip";
      tip.textContent = item.storage_tip;
      main.appendChild(tip);
    }

    var side = document.createElement("div");
    side.className = "card__side";
    side.appendChild(pill(item.change_pct));

    var pickBtn = document.createElement("button");
    pickBtn.type = "button";
    pickBtn.className = "pick-btn";
    // Tag the card with its ingredient name so syncSelectionUI() can derive
    // every visible card's picked state from `selected` (one shared basket).
    li.setAttribute("data-name", item.name);

    pickBtn.addEventListener("click", function (event) {
      // Must not trigger the card's tap-to-expand.
      event.stopPropagation();
      toggleSelected(item.name);
    });

    side.appendChild(pickBtn);

    head.appendChild(main);
    head.appendChild(side);

    // ----- detail -----
    var detail = document.createElement("div");
    detail.className = "card__detail";

    var rLabel = document.createElement("p");
    rLabel.className = "detail__label";
    rLabel.textContent = "이걸 추천하는 이유";
    detail.appendChild(rLabel);

    var reasons = document.createElement("ul");
    reasons.className = "reasons";
    (item.reasons || []).forEach(function (r) {
      var liR = document.createElement("li");
      liR.textContent = r;
      reasons.appendChild(liR);
    });
    detail.appendChild(reasons);

    var baseline = document.createElement("p");
    baseline.className = "detail__baseline";
    baseline.textContent = "KAMIS 전국 평균 소매 기준가 · 변동률은 평소 대비";
    detail.appendChild(baseline);

    if (item.storage_tip) {
      var tLabel = document.createElement("p");
      tLabel.className = "detail__label";
      tLabel.textContent = "보관 팁";
      detail.appendChild(tLabel);

      var tipBox = document.createElement("div");
      tipBox.className = "detail__tip";
      tipBox.textContent = item.storage_tip;
      detail.appendChild(tipBox);
    }

    var lLabel = document.createElement("p");
    lLabel.className = "detail__label";
    lLabel.textContent = "레시피 검색";
    detail.appendChild(lLabel);

    var links = document.createElement("div");
    links.className = "recipe-links";
    (item.recipe_links || []).forEach(function (link) {
      var a = document.createElement("a");
      a.className = "recipe-btn";
      a.href = link.url;
      a.target = "_blank";
      a.rel = "noopener";
      a.textContent = link.label;
      links.appendChild(a);
    });
    detail.appendChild(links);

    li.appendChild(head);
    li.appendChild(detail);

    head.addEventListener("click", function () {
      li.classList.toggle("is-open");
    });

    return li;
  }

  function renderActionbar() {
    var n = selected.length;
    els.actionbar.hidden = n === 0;
    document.body.classList.toggle("has-actionbar", n > 0);
    els.actionbarCount.textContent = "재료 " + n + "개 선택";
  }

  // Single source of truth for "what is picked". Re-derives, from `selected`,
  // every #recmenu chip's selected style, every visible card's
  // .pick-btn/.is-picked, the recmenu CTA enabled state, and the action bar.
  // Card 담기 and the recmenu chip for the same ingredient always reflect the
  // same state because both read this one `selected` array. Call after ANY
  // selection change (chip toggle, card toggle, clear) and at end of
  // render()/renderCards().
  function syncSelectionUI() {
    // recmenu chips
    var chips = els.recmenuChips.querySelectorAll(".recmenu__chip");
    chips.forEach(function (chip) {
      var on = selected.indexOf(chip.getAttribute("data-name")) !== -1;
      chip.classList.toggle("is-selected", on);
      chip.setAttribute("aria-pressed", on ? "true" : "false");
    });
    // visible cards
    var cards = els.cards.querySelectorAll(".card");
    cards.forEach(function (card) {
      var on = selected.indexOf(card.getAttribute("data-name")) !== -1;
      card.classList.toggle("is-picked", on);
      var b = card.querySelector(".pick-btn");
      if (b) {
        b.textContent = on ? "담음 ✓" : "담기";
        b.setAttribute("aria-pressed", on ? "true" : "false");
      }
    });
    // recmenu CTA: disabled at 0 (the action bar already hides at 0).
    if (els.recmenuGo) els.recmenuGo.disabled = selected.length === 0;
    renderActionbar();
  }

  // Add/remove a name from the one shared basket, then resync all UI.
  function toggleSelected(name) {
    var idx = selected.indexOf(name);
    if (idx === -1) {
      selected.push(name);
    } else {
      selected.splice(idx, 1);
    }
    syncSelectionUI();
  }

  function clearSelection() {
    selected = [];
    syncSelectionUI();
  }

  function closeSheet() {
    els.comboSheet.hidden = true;
    els.comboBody.innerHTML = "";
  }

  function renderComboSheet(data) {
    els.comboBody.innerHTML = "";

    if (data.all_staple === true) {
      var hint = document.createElement("p");
      hint.className = "sheet__hint";
      hint.textContent =
        "대파·양파 같은 기본 재료만 골랐어요. 고기·생선·두부 같은 주재료를 하나 더하면 레시피가 더 잘 맞아요.";
      els.comboBody.appendChild(hint);

      var sugLabel = document.createElement("p");
      sugLabel.className = "sheet__suglabel";
      sugLabel.textContent = "기본 재료만으로 되는 요리";
      els.comboBody.appendChild(sugLabel);

      var sugList = document.createElement("div");
      sugList.className = "sheet__suggestions";
      (data.suggestions || []).forEach(function (s) {
        var a = document.createElement("a");
        a.className = "sheet__sug";
        a.href = s.url;
        a.target = "_blank";
        a.rel = "noopener";
        a.textContent = s.name;
        sugList.appendChild(a);
      });
      els.comboBody.appendChild(sugList);
    }

    var chips = document.createElement("div");
    chips.className = "sheet__chips";
    (data.items || []).forEach(function (name) {
      var c = document.createElement("span");
      c.className = "sheet__chip";
      c.textContent = name;
      chips.appendChild(c);
    });
    els.comboBody.appendChild(chips);

    // Below the chips: a container the inline real results (3 source
    // sections) fill in. Until /api/recipe-results resolves it shows a
    // brief loading line; on any failure it falls back to the existing
    // deep-link buttons (data.recipe_links) — the unchanged old behaviour.
    var rrWrap = document.createElement("div");
    rrWrap.className = "rr-wrap";
    var rrLoading = document.createElement("p");
    rrLoading.className = "sheet__msg rr-loading";
    rrLoading.textContent = "레시피 검색 결과 불러오는 중…";
    rrWrap.appendChild(rrLoading);
    els.comboBody.appendChild(rrWrap);

    return rrWrap;
  }

  // The pre-existing 3-button deep-link row. This IS the graceful fallback:
  // used when /api/recipe-results fails entirely (network/HTTP error).
  function buildFallbackLinks(recipeLinks) {
    var links = document.createElement("div");
    links.className = "recipe-links";
    (recipeLinks || []).forEach(function (link) {
      var a = document.createElement("a");
      a.className = "recipe-btn";
      a.href = link.url;
      a.target = "_blank";
      a.rel = "noopener";
      a.textContent = link.label;
      links.appendChild(a);
    });
    return links;
  }

  // Render the 3 source sections from /api/recipe-results into `wrap`.
  // status "ok" + results -> a vertical list of result cards + a muted
  // "더 보기" link. status "fallback"/no results -> a single deep-link
  // button (label like "만개의레시피에서 검색") == the old behaviour.
  function renderRecipeResults(wrap, rrData) {
    wrap.innerHTML = "";
    var sources = (rrData && rrData.sources) || [];
    sources.forEach(function (src) {
      var section = document.createElement("div");
      section.className = "rr-section";

      var h = document.createElement("p");
      h.className = "rr-head";
      h.textContent = src.label;
      section.appendChild(h);

      var hasResults =
        src.status === "ok" && src.results && src.results.length > 0;

      if (hasResults) {
        var list = document.createElement("div");
        list.className = "rr-list";
        src.results.forEach(function (r) {
          var a = document.createElement("a");
          a.className = "rr-card";
          a.href = r.url;
          a.target = "_blank";
          a.rel = "noopener";

          if (r.thumbnail) {
            var img = document.createElement("img");
            img.className = "rr-thumb";
            img.src = r.thumbnail;
            img.loading = "lazy";
            img.alt = "";
            a.appendChild(img);
          }

          var body = document.createElement("div");
          body.className = "rr-cardbody";
          var t = document.createElement("div");
          t.className = "rr-title";
          t.textContent = r.title || "";
          body.appendChild(t);
          var sub = r.channel || r.desc;
          if (sub) {
            var s = document.createElement("div");
            s.className = "rr-sub";
            s.textContent = sub;
            body.appendChild(s);
          }
          a.appendChild(body);
          list.appendChild(a);
        });
        section.appendChild(list);

        if (src.more_url) {
          var more = document.createElement("a");
          more.className = "rr-more";
          more.href = src.more_url;
          more.target = "_blank";
          more.rel = "noopener";
          more.textContent = "더 보기";
          section.appendChild(more);
        }
      } else {
        // Graceful fallback: the current single deep-link button.
        var btn = document.createElement("a");
        btn.className = "recipe-btn";
        btn.href = src.more_url || "#";
        btn.target = "_blank";
        btn.rel = "noopener";
        btn.textContent = src.label + "에서 검색";
        section.appendChild(btn);
      }

      wrap.appendChild(section);
    });
  }

  function showComboMessage(text) {
    els.comboBody.innerHTML = "";
    var p = document.createElement("p");
    p.className = "sheet__msg";
    p.textContent = text;
    els.comboBody.appendChild(p);
  }

  // Open the combo sheet for an explicit list of ingredient names. Used by
  // both the action bar (selected[]) and the recommended-menu button — the
  // latter must NOT touch `selected`, so the name list is passed in.
  function openComboFor(names) {
    if (!names || names.length === 0) return;
    els.comboSheet.hidden = false;
    showComboMessage("레시피 찾는 중…");

    var q = names
      .map(function (n) {
        return encodeURIComponent(n);
      })
      .join(",");

    fetch("/api/combo-recipes?items=" + q)
      .then(function (res) {
        return res.json().then(function (body) {
          return { ok: res.ok, body: body };
        });
      })
      .then(function (r) {
        if (!r.ok) {
          showComboMessage(
            (r.body && r.body.detail) || "레시피를 불러오지 못했어요."
          );
          return;
        }
        // chips + all_staple hint + suggestions exactly as before; returns
        // the container the inline real results fill in below the chips.
        var rrWrap = renderComboSheet(r.body);
        var fallbackLinks = (r.body && r.body.recipe_links) || [];

        // THEN fetch the inline real results. Any failure -> keep the old
        // deep-link buttons so the sheet always works (no console errors).
        fetch("/api/recipe-results?items=" + q)
          .then(function (res2) {
            if (!res2.ok) throw new Error("HTTP " + res2.status);
            return res2.json();
          })
          .then(function (rrData) {
            if (!rrWrap || !rrWrap.isConnected) return;
            renderRecipeResults(rrWrap, rrData);
          })
          .catch(function () {
            if (!rrWrap || !rrWrap.isConnected) return;
            rrWrap.innerHTML = "";
            rrWrap.appendChild(buildFallbackLinks(fallbackLinks));
          });
      })
      .catch(function () {
        showComboMessage("레시피를 불러오지 못했어요. 잠시 후 다시 시도해 주세요.");
      });
  }

  function openCombo() {
    openComboFor(selected);
  }

  function show(state) {
    els.loading.hidden = state !== "loading";
    els.error.hidden = state !== "error";
    els.cards.hidden = state !== "cards";
    // Filter chips + sort selector only make sense once cards are present.
    els.filters.hidden = state !== "cards";
    els.sorts.hidden = state !== "cards";
    if (state !== "cards") {
      els.feedEmpty.hidden = true;
      els.recmenu.hidden = true;
    }
  }

  // change_pct can be null (no baseline). For "cheapest vs usual" ordering a
  // null must rank LAST, so treat it as +Infinity (worse than any rise).
  function changeKey(item) {
    var c = item.change_pct;
    return c === null || c === undefined ? Infinity : c;
  }

  // Sort a copy of `arr` by the active sort key. Pure (no mutation of input).
  function sortItems(arr) {
    var out = arr.slice();
    if (activeSort === "price") {
      out.sort(function (a, b) {
        return (a.price || 0) - (b.price || 0);
      });
    } else if (activeSort === "name") {
      out.sort(function (a, b) {
        return String(a.name).localeCompare(String(b.name), "ko");
      });
    } else if (activeSort === "score") {
      out.sort(function (a, b) {
        return (b.score || 0) - (a.score || 0);
      });
    } else {
      // "discount" (default): most-negative change_pct first, nulls last.
      out.sort(function (a, b) {
        return changeKey(a) - changeKey(b);
      });
    }
    return out;
  }

  // Pick the 3 "평소보다 가장 싸진" items globally (independent of the filter):
  // sort all items by change_pct ascending with nulls treated as worst/last,
  // then take the first 3. Stores names in `top3Names`.
  function computeTop3() {
    var ranked = allItems.slice().sort(function (a, b) {
      return changeKey(a) - changeKey(b);
    });
    var picks = ranked.slice(0, 3);
    top3Names = picks.map(function (it) {
      return it.name;
    });
    return picks;
  }

  function renderRecmenu() {
    var picks = computeTop3();
    if (picks.length === 0) {
      els.recmenu.hidden = true;
      els.recmenuChips.innerHTML = "";
      return;
    }
    els.recmenuChips.innerHTML = "";
    picks.forEach(function (it) {
      // Toggle chip: adds/removes this ingredient from the SAME `selected[]`
      // the card 담기 uses (one shared basket). State is painted by
      // syncSelectionUI(), so card and chip always stay in sync.
      var chip = document.createElement("button");
      chip.type = "button";
      chip.className = "recmenu__chip";
      chip.setAttribute("data-name", it.name);
      chip.setAttribute("aria-pressed", "false");
      chip.appendChild(document.createTextNode(it.name));
      if (it.change_pct !== null && it.change_pct !== undefined) {
        var pct = document.createElement("span");
        pct.className = "recmenu__pct";
        pct.textContent = "▼" + Math.abs(Math.round(it.change_pct)) + "%";
        chip.appendChild(pct);
      }
      chip.addEventListener("click", function () {
        toggleSelected(it.name);
      });
      els.recmenuChips.appendChild(chip);
    });
    els.recmenu.hidden = false;
  }

  // Paint the cards for the active group. The fetched list is preserved — we
  // only filter+sort the view (no API re-call). Selection is by name, so it
  // persists across filter/sort changes even for hidden items.
  function renderCards() {
    els.cards.innerHTML = "";

    var filtered =
      activeGroup === "전체"
        ? allItems
        : allItems.filter(function (item) {
            return item.group === activeGroup;
          });

    // Sort the full group BEFORE the overview cap so "전체" = first 15 of the
    // fully-sorted list, and a category tab = all of that category, sorted.
    var sorted = sortItems(filtered);
    var visible =
      activeGroup === "전체" ? sorted.slice(0, OVERVIEW_LIMIT) : sorted;

    if (visible.length === 0) {
      els.feedEmpty.hidden = false;
      return;
    }
    els.feedEmpty.hidden = true;
    visible.forEach(function (item) {
      els.cards.appendChild(buildCard(item));
    });
    // Freshly-built cards must reflect the shared basket immediately.
    syncSelectionUI();
  }

  function setActiveSort(sort) {
    activeSort = sort;
    var btns = els.sorts.querySelectorAll(".sortbtn");
    btns.forEach(function (btn) {
      var on = btn.getAttribute("data-sort") === sort;
      btn.classList.toggle("is-active", on);
      btn.setAttribute("aria-pressed", on ? "true" : "false");
    });
    renderCards();
  }

  function setActiveGroup(group) {
    activeGroup = group;
    var chips = els.filters.querySelectorAll(".chip");
    chips.forEach(function (chip) {
      var on = chip.getAttribute("data-group") === group;
      chip.classList.toggle("is-active", on);
      chip.setAttribute("aria-pressed", on ? "true" : "false");
    });
    renderCards();
  }

  function render(data) {
    els.date.textContent = formatDate(data.date);
    els.sampleBadge.hidden = data.source !== "sample";

    if (!data.items || data.items.length === 0) {
      allItems = [];
      top3Names = [];
      els.cards.innerHTML = "";
      els.recmenu.hidden = true;
      selected = [];
      renderActionbar();
      show("error");
      return;
    }

    // Fresh fetch — keep full list & reset selection to stay in sync.
    allItems = data.items;
    selected = [];
    activeSort = "discount";
    // Recompute the "평소보다 가장 싸진 3가지" block from the fresh global list
    // (this also fills top3Names via computeTop3()).
    renderRecmenu();
    // Default: start the shared basket with the 3 cheapest so the action bar
    // shows "재료 3개 선택" on load. Card 담기 / recmenu chips toggle the SAME
    // list from here.
    selected = top3Names.slice();
    // Sync the sort buttons to the reset default, then paint "전체".
    setActiveSort("discount");
    // "전체" always has items, so this never lands on the empty message.
    setActiveGroup("전체");
    // Final resync so recmenu chips + visible cards + action bar all reflect
    // the preselected top3 (renderCards already calls this, but be explicit).
    syncSelectionUI();
    show("cards");
  }

  function load() {
    show("loading");
    fetch("/api/recommendations?limit=0")
      .then(function (res) {
        if (!res.ok) throw new Error("HTTP " + res.status);
        return res.json();
      })
      .then(render)
      .catch(function () {
        show("error");
      });
  }

  els.retry.addEventListener("click", load);

  els.filters.addEventListener("click", function (event) {
    var chip = event.target.closest(".chip");
    if (!chip || !els.filters.contains(chip)) return;
    var group = chip.getAttribute("data-group");
    if (group && group !== activeGroup) setActiveGroup(group);
  });

  els.sorts.addEventListener("click", function (event) {
    var btn = event.target.closest(".sortbtn");
    if (!btn || !els.sorts.contains(btn)) return;
    var sort = btn.getAttribute("data-sort");
    if (sort && sort !== activeSort) setActiveSort(sort);
  });

  els.recmenuGo.addEventListener("click", function () {
    // Same path as the action bar — recipes for the one shared basket.
    openComboFor(selected);
  });

  els.actionbarClear.addEventListener("click", clearSelection);
  els.actionbarGo.addEventListener("click", openCombo);
  els.comboClose.addEventListener("click", closeSheet);
  els.comboBackdrop.addEventListener("click", closeSheet);

  load();
})();
