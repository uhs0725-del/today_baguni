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
    recmenuShare: document.getElementById("recmenu-share"),
    funbar: document.getElementById("funbar"),
    dailyTip: document.getElementById("daily-tip"),
    rollBtn: document.getElementById("roll-btn"),
    rollPop: document.getElementById("roll-pop"),
    rollBackdrop: document.getElementById("roll-backdrop"),
    rollClose: document.getElementById("roll-close"),
    rollPick: document.getElementById("roll-pick"),
    rollAgain: document.getElementById("roll-again"),
    rollRecipe: document.getElementById("roll-recipe"),
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
    exitToast: document.getElementById("exit-toast"),
    searchbar: document.getElementById("searchbar"),
    searchInput: document.getElementById("search-input"),
    searchClear: document.getElementById("search-clear"),
  };

  // Selected ingredient names (server resolves combo recipes by name).
  var selected = [];

  // Full fetched+ranked list (order preserved) and the active filter group.
  // "전체" shows a ranked shortlist (decision-reduction); a category chip
  // shows that whole category. Filtering is client-side (no re-fetch).
  var allItems = [];
  var activeGroup = "전체";
  var OVERVIEW_LIMIT = 15;

  // Free-text ingredient search. When non-empty it OVERRIDES the category chip
  // AND the "전체" overview cap: matches by name across the whole list so any
  // ingredient is findable from any tab. Empty = normal category view.
  var searchQuery = "";

  // Sort applied inside the card list. Default mirrors the header sub-copy
  // ("평소보다 싸고"): cheapest-vs-usual first. Independent of the filter chip.
  var activeSort = "discount";

  // The 3 "평소보다 가장 싸진" names backing the recommended-menu block.
  // Computed globally from allItems (NOT affected by the active filter).
  var top3Names = [];

  // 음료 tab: prices come from NAVER 쇼핑 (NOT KAMIS), so this tab renders a
  // DISTINCT list and has NO ▼%/제철/담기/expand. Fetched once per page load
  // and cached here; null = not fetched yet, [] = fetched-but-empty.
  var bevItems = null;
  var bevLoading = false;

  // Online lowest price (NAVER 쇼핑) per ingredient, fetched LAZILY when a
  // card is first expanded — calling it for every recommendation on load
  // would blow the NAVER quota. name -> "loading" while in flight, else the
  // fetched response object (kept so re-expanding / another card with the
  // same name never refetches). DELIBERATELY separate from the KAMIS ▼%
  // signal: the price here is a NAVER 쇼핑 lowest listing (often multipack).
  var onlinePriceCache = {};

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
    // Short weekday ("수") so the date fits inline next to the title on
    // small mobile widths without wrapping. (Was "수요일".)
    return (
      d.getMonth() + 1 + "월 " + d.getDate() + "일 " + WEEKDAYS[d.getDay()]
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

    // ----- online lowest price (NAVER 쇼핑) — lazy, distinct basis -----
    // Deliberately BELOW the KAMIS reasons/baseline and inside the expand
    // so it reads as a separate, different-unit data point (often a
    // multipack), never as part of the always-visible KAMIS ▼% signal.
    var oLabel = document.createElement("p");
    oLabel.className = "detail__label";
    oLabel.textContent = "온라인 최저가";
    detail.appendChild(oLabel);

    var onlineBox = document.createElement("div");
    onlineBox.className = "online-box";
    var onlineLoading = document.createElement("div");
    onlineLoading.className = "online-loading";
    onlineLoading.textContent = "네이버 가격 불러오는 중…";
    onlineBox.appendChild(onlineLoading);
    detail.appendChild(onlineBox);

    // Subtle "there's more inside" affordance. Wording is GENERIC ("자세히")
    // on purpose: the expand also holds solid content (추천 이유·보관 팁·
    // 레시피), while the online price is noisy — so we hint at detail in
    // general, never promise "온라인 최저가" up front. Hidden once open.
    var moreHint = document.createElement("div");
    moreHint.className = "card__more";
    moreHint.setAttribute("aria-hidden", "true");
    var moreLabel = document.createElement("span");
    moreLabel.className = "card__more-label";
    moreLabel.textContent = "자세히";
    var moreChev = document.createElement("span");
    moreChev.className = "card__more-chev";
    moreChev.textContent = "▾";
    moreHint.appendChild(moreLabel);
    moreHint.appendChild(moreChev);

    // moreHint sits at the BOTTOM of the card (not between head and
    // detail) so when expanded it stays under the detail and works as a
    // visible "접기 ▴" close affordance; when collapsed it appears right
    // under the head (detail is display:none) as the "자세히 ▾" cue.
    li.appendChild(head);
    li.appendChild(detail);
    li.appendChild(moreHint);

    // Toggle open/closed. Lazy-fetch the online price the FIRST time this
    // card opens (and only on open) — cached by name so re-expanding (or
    // another card with the same ingredient) never refetches / re-hits the
    // quota. The label swaps and the chevron flips (CSS) for the open
    // affordance.
    function toggleOpen() {
      li.classList.toggle("is-open");
      var open = li.classList.contains("is-open");
      moreLabel.textContent = open ? "접기" : "자세히";
      if (open) {
        loadOnlinePrice(item.name, onlineBox);
      }
    }
    head.addEventListener("click", toggleOpen);
    moreHint.addEventListener("click", toggleOpen);

    return li;
  }

  // NAVER 쇼핑 search deep-link for a name (used as the always-available
  // fallback link when the per-ingredient fetch has no usable response).
  function naverShopSearchUrl(name) {
    return (
      "https://search.shopping.naver.com/search/all?query=" +
      encodeURIComponent(name)
    );
  }

  // Render a successful /api/online-price response into `box`. The whole
  // block is a link into NAVER 쇼핑 (data.url, else data.more_url). NO ▼% /
  // no "vs 기준가" is shown — the raw listing title is surfaced so a
  // multipack/대용량 unit is transparent (honest, separate from KAMIS).
  function renderOnlinePrice(box, data) {
    box.innerHTML = "";

    var hasPrice = data.price !== null && data.price !== undefined;
    var href = data.url || data.more_url || "#";

    var a = document.createElement("a");
    a.className = "online-link";
    a.href = href;
    a.target = "_blank";
    a.rel = "noopener";

    var badge = document.createElement("span");
    badge.className = "online-badge";
    badge.textContent = "네이버 쇼핑 최저가";
    a.appendChild(badge);

    var price = document.createElement("div");
    price.className = "online-price";
    price.textContent = hasPrice
      ? "약 " + Number(data.price).toLocaleString("ko-KR") + "원~"
      : "가격 확인";
    a.appendChild(price);

    if (data.listing) {
      var listing = document.createElement("div");
      listing.className = "online-listing";
      listing.textContent = data.listing;
      a.appendChild(listing);
    }

    if (data.mall) {
      var mall = document.createElement("div");
      mall.className = "online-mall";
      mall.textContent = data.mall;
      a.appendChild(mall);
    }

    if (!hasPrice) {
      var see = document.createElement("div");
      see.className = "online-mall";
      see.textContent = "네이버 쇼핑에서 보기";
      a.appendChild(see);
    }

    box.appendChild(a);

    // Explicit NAVER 가격비교 (all-sellers) deep-link — a sibling of the
    // representative-listing block (NOT nested: avoids invalid nested <a>).
    // This is where 쿠팡 등 다른 판매처 prices surface (NAVER aggregates
    // them); the user picks their own size/seller there.
    var cmp = document.createElement("a");
    cmp.className = "online-compare";
    cmp.href = data.more_url || naverShopSearchUrl(data.name || "");
    cmp.target = "_blank";
    cmp.rel = "noopener";
    cmp.textContent = "네이버 가격비교 · 쿠팡 등 전체 판매처 보기";
    box.appendChild(cmp);

    var caption = document.createElement("div");
    caption.className = "online-caption";
    caption.textContent =
      "네이버쇼핑 온라인 최저가 · 묶음/대용량일 수 있어 KAMIS 기준가와 단위가 달라요";
    box.appendChild(caption);
  }

  // Replace the placeholder with a single NAVER 쇼핑 search deep-link —
  // the graceful fallback when the fetch fails or returns nothing usable.
  function renderOnlineFallback(box, name, moreUrl) {
    box.innerHTML = "";
    var btn = document.createElement("a");
    btn.className = "online-compare";
    btn.href = moreUrl || naverShopSearchUrl(name);
    btn.target = "_blank";
    btn.rel = "noopener";
    btn.textContent = "네이버 가격비교 · 전체 판매처 보기";
    box.appendChild(btn);
  }

  // Lazy fetch the online price for `name` and render into `box`. Cached by
  // name (re-expand / same-ingredient card never refetches). Defensive like
  // the combo lazy-fetch: never throws, never console.error — any failure
  // degrades to a single NAVER 쇼핑 search deep-link.
  function loadOnlinePrice(name, box) {
    var cached = onlinePriceCache[name];
    if (cached === "loading") return;
    if (cached) {
      if (cached.status === "ok") {
        renderOnlinePrice(box, cached);
      } else {
        renderOnlineFallback(box, name, cached.more_url);
      }
      return;
    }

    onlinePriceCache[name] = "loading";
    fetch("/api/online-price?name=" + encodeURIComponent(name))
      .then(function (res) {
        if (!res.ok) throw new Error("HTTP " + res.status);
        return res.json();
      })
      .then(function (data) {
        onlinePriceCache[name] = data;
        if (!box || !box.isConnected) return;
        if (data && data.status === "ok") {
          renderOnlinePrice(box, data);
        } else {
          renderOnlineFallback(box, name, data && data.more_url);
        }
      })
      .catch(function () {
        // Don't pin a failure in the cache — a later expand may succeed.
        onlinePriceCache[name] = null;
        if (!box || !box.isConnected) return;
        renderOnlineFallback(box, name, null);
      });
  }

  function renderActionbar() {
    var n = selected.length;
    els.actionbar.hidden = n === 0;
    document.body.classList.toggle("has-actionbar", n > 0);
    els.actionbarCount.textContent = "재료 " + n + "개 선택";
  }

  // Single source of truth for "what is picked". Re-derives, from `selected`,
  // the WHOLE recmenu basket (auto recommendations + user-added), every
  // visible card's .pick-btn/.is-picked, the recmenu CTA enabled state, and
  // the action bar. Card 담기 and the recmenu chip for the same ingredient
  // always reflect the same state because both read this one `selected`
  // array. Call after ANY selection change (chip toggle, card toggle, clear)
  // and at end of render()/renderCards().
  function syncSelectionUI() {
    // recmenu chips: full re-render so user-added chips appear/disappear by
    // membership and every chip gets its correct per-state style.
    paintRecmenuChips();
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

    // Share THIS selection's recipes. The shared link carries the chosen
    // ingredients (?items=…) so the friend's app opens with them pre-
    // selected and the recipe sheet auto-open — they see the same recipes.
    var shareNames = (data.items || []).slice();
    if (shareNames.length) {
      var rsBtn = document.createElement("button");
      rsBtn.type = "button";
      rsBtn.className = "sheet__share";
      rsBtn.textContent = "📋 이 재료 레시피 친구에게 공유";
      rsBtn.addEventListener("click", function () {
        shareRecipeCard(shareNames, rsBtn);
      });
      els.comboBody.appendChild(rsBtn);
    }

    // Below the chips: a container the inline real results (3 source
    // sections) fill in. Until /api/recipe-results resolves it shows a
    // brief loading line; on any failure it falls back to the existing
    // deep-link buttons (data.recipe_links) — the unchanged old behaviour.
    var rrWrap = document.createElement("div");
    rrWrap.className = "rr-wrap";
    rrWrap.appendChild(buildRecipeSkeleton());
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

    // Across all sources: did anything match in-app? If NOT (the strict-
    // filter rare-combo case), show one clear banner up top — right under
    // the selected-ingredient chips — so the "X에서 검색" links below read
    // as an intentional fallback, not a glitch.
    var okCount = sources.filter(function (s) {
      return s.status === "ok" && s.results && s.results.length > 0;
    }).length;
    var allEmpty = sources.length > 0 && okCount === 0;
    if (allEmpty) {
      var banner = document.createElement("p");
      banner.className = "rr-allempty";
      banner.textContent =
        "😅 이 재료 조합엔 딱 맞는 레시피가 없어요. 아래에서 직접 검색해 보세요.";
      wrap.appendChild(banner);
    }

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
        // No in-app results for THIS source. In the MIXED case (some other
        // source DID have results) flag this one per-source with an emoji;
        // when EVERYTHING is empty the top banner already says it, so we
        // skip the per-source note and just show the search fallback.
        if (!allEmpty) {
          var empty = document.createElement("p");
          empty.className = "rr-empty";
          empty.textContent = "😶 이 재료론 레시피가 없어요";
          section.appendChild(empty);
        }
        // Graceful fallback: the external-search deep-link button.
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

  function showComboMessage(text, loading) {
    els.comboBody.innerHTML = "";
    if (loading) {
      var sp = document.createElement("div");
      sp.className = "spinner";
      sp.setAttribute("aria-hidden", "true");
      els.comboBody.appendChild(sp);
    }
    var p = document.createElement("p");
    p.className = "sheet__msg";
    p.textContent = text;
    els.comboBody.appendChild(p);
  }

  // Animated skeleton shown while /api/recipe-results loads. That fetch can
  // take several seconds on a cold cache (we verify each recipe actually
  // contains the chosen ingredients), so a static line read as "frozen".
  // The shimmer + rotating message make it clearly "working". The interval
  // self-clears once the node is replaced by the real results or fallback
  // (msg.isConnected === false).
  function buildRecipeSkeleton() {
    var box = document.createElement("div");
    box.className = "rr-skel";

    var msg = document.createElement("p");
    msg.className = "rr-skel-msg";
    var msgs = [
      "레시피 찾는 중…",
      "재료가 실제로 들어간 것만 골라요",
      "거의 다 됐어요…",
    ];
    var mi = 0;
    msg.textContent = msgs[0];
    box.appendChild(msg);

    var timer = setInterval(function () {
      if (!msg.isConnected) {
        clearInterval(timer);
        return;
      }
      mi = (mi + 1) % msgs.length;
      msg.textContent = msgs[mi];
    }, 2400);

    for (var i = 0; i < 3; i++) {
      var card = document.createElement("div");
      card.className = "rr-skel-card";
      var thumb = document.createElement("div");
      thumb.className = "skeleton rr-skel-thumb";
      var lines = document.createElement("div");
      lines.className = "rr-skel-lines";
      var l1 = document.createElement("div");
      l1.className = "skeleton rr-skel-line";
      var l2 = document.createElement("div");
      l2.className = "skeleton rr-skel-line rr-skel-line--short";
      lines.appendChild(l1);
      lines.appendChild(l2);
      card.appendChild(thumb);
      card.appendChild(lines);
      box.appendChild(card);
    }
    return box;
  }

  // Open the combo sheet for an explicit list of ingredient names. Used by
  // both the action bar (selected[]) and the recommended-menu button — the
  // latter must NOT touch `selected`, so the name list is passed in.
  function openComboFor(names) {
    if (!names || names.length === 0) return;
    els.comboSheet.hidden = false;
    showComboMessage("레시피 찾는 중…", true);

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
    // Search box + filter chips + sort selector only make sense once cards are present.
    els.searchbar.hidden = state !== "cards";
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

  // Look up an item by name in the full fetched list (for the price-change
  // suffix on a chip). Returns undefined if not found.
  function itemByName(name) {
    for (var i = 0; i < allItems.length; i++) {
      if (allItems[i].name === name) return allItems[i];
    }
    return undefined;
  }

  // Build one toggle chip. `kind` is one of:
  //   "rec-on"  : auto recommendation, currently selected (recommended look)
  //   "rec-off" : auto recommendation, de-selected (outline/muted look,
  //               still tappable to re-add — it's a standing recommendation)
  //   "pick"    : user-added (in `selected`, NOT a top3 recommendation) —
  //               distinct color; only ever rendered while selected
  // Tapping toggles membership in the SAME shared `selected[]` the card 담기
  // uses, then syncs all UI so card and chip never diverge.
  function buildRecmenuChip(name, kind) {
    var chip = document.createElement("button");
    chip.type = "button";
    chip.className = "recmenu__chip";
    if (kind === "rec-on") {
      chip.className += " is-selected";
    } else if (kind === "pick") {
      chip.className += " is-selected recmenu__chip--pick";
    } else {
      // "rec-off": de-selected standing recommendation (outline look).
      chip.className += " recmenu__chip--rec-off";
    }
    chip.setAttribute("data-name", name);
    chip.setAttribute(
      "aria-pressed",
      kind === "rec-off" ? "false" : "true"
    );
    chip.appendChild(document.createTextNode(name));
    var it = itemByName(name);
    if (it && it.change_pct !== null && it.change_pct !== undefined) {
      var pct = document.createElement("span");
      pct.className = "recmenu__pct";
      pct.textContent = "▼" + Math.abs(Math.round(it.change_pct)) + "%";
      chip.appendChild(pct);
    }
    chip.addEventListener("click", function () {
      toggleSelected(name);
    });
    return chip;
  }

  // Re-render #recmenu-chips as the WHOLE basket, in order: every auto
  // recommendation (top3Names, in their existing order) — selected or not —
  // then any USER-ADDED selected ingredient (in `selected` but NOT a top3),
  // in selection order. Auto+selected -> recommended look; auto+deselected ->
  // outline look; user-added -> distinct "내가 담음" color. Pure paint from
  // `selected` + `top3Names`; safe to call on every selection change.
  function paintRecmenuChips() {
    els.recmenuChips.innerHTML = "";
    top3Names.forEach(function (name) {
      var on = selected.indexOf(name) !== -1;
      els.recmenuChips.appendChild(
        buildRecmenuChip(name, on ? "rec-on" : "rec-off")
      );
    });
    selected.forEach(function (name) {
      if (top3Names.indexOf(name) === -1) {
        els.recmenuChips.appendChild(buildRecmenuChip(name, "pick"));
      }
    });
  }

  // Compute the recommended 3, decide block visibility, then paint the union.
  function renderRecmenu() {
    var picks = computeTop3();
    if (picks.length === 0) {
      els.recmenu.hidden = true;
      els.recmenuChips.innerHTML = "";
      if (els.funbar) els.funbar.hidden = true;
      return;
    }
    paintRecmenuChips();
    els.recmenu.hidden = false;
    if (els.funbar) els.funbar.hidden = false;
    setDailyTip();
  }

  // ---------------------------------------------------------------------
  // 음료 (NAVER 쇼핑) — a deliberately DISTINCT card so users never confuse
  // a NAVER lowest-listing price (often a multipack) with the KAMIS ▼%
  // signal. No 담기, no ▼% pill, no 제철/solo badge, no expand. The whole
  // card is a link into NAVER 쇼핑 (item.url, else item.more_url).
  // ---------------------------------------------------------------------
  function buildBevCard(item) {
    var a = document.createElement("a");
    a.className = "bev-card";
    a.href = item.url || item.more_url || "#";
    a.target = "_blank";
    a.rel = "noopener";

    var name = document.createElement("div");
    name.className = "bev-name";
    name.textContent = item.name;
    a.appendChild(name);

    var badge = document.createElement("span");
    badge.className = "bev-badge";
    badge.textContent = "네이버 쇼핑 최저가";
    a.appendChild(badge);

    var price = document.createElement("div");
    price.className = "bev-price";
    if (item.price === null || item.price === undefined) {
      price.textContent = "가격 확인";
    } else {
      price.textContent =
        "약 " + Number(item.price).toLocaleString("ko-KR") + "원~";
    }
    a.appendChild(price);

    if (item.listing) {
      var listing = document.createElement("div");
      listing.className = "bev-listing";
      listing.textContent = item.listing;
      a.appendChild(listing);
    }

    if (item.mall) {
      var mall = document.createElement("div");
      mall.className = "bev-mall";
      mall.textContent = item.mall;
      a.appendChild(mall);
    }

    return a;
  }

  // Render the cached beverage list into #cards (a one-line notice bar on
  // top). Distinct from the KAMIS card path. `state` is "loading" | "list".
  function renderBeverages(state) {
    els.cards.innerHTML = "";
    els.feedEmpty.hidden = true;

    var notice = document.createElement("li");
    notice.className = "bev-notice";
    notice.textContent =
      "음료는 네이버 쇼핑 최저가 기준이에요 — 채소·고기 시세(▼%)와 기준이 달라요.";
    els.cards.appendChild(notice);

    if (state === "loading") {
      var loadingLi = document.createElement("li");
      loadingLi.className = "bev-loading";
      loadingLi.textContent = "음료 가격 불러오는 중…";
      els.cards.appendChild(loadingLi);
      return;
    }

    if (!bevItems || bevItems.length === 0) {
      var emptyLi = document.createElement("li");
      emptyLi.className = "bev-loading";
      emptyLi.textContent = "음료 정보를 불러오지 못했어요.";
      els.cards.appendChild(emptyLi);
      return;
    }

    bevItems.forEach(function (it) {
      els.cards.appendChild(buildBevCard(it));
    });
  }

  // Show the 음료 tab: hide the produce-only UI (recmenu/sorts), then show
  // the beverage list. Fetch /api/beverages once per load (cache in
  // bevItems); a brief loading state shows in the feed meanwhile. Any
  // failure → an empty-notice list (the notice bar always renders).
  function showBeveragesTab() {
    els.recmenu.hidden = true;
    els.sorts.hidden = true;
    els.searchbar.hidden = true;

    if (bevItems !== null) {
      renderBeverages("list");
      return;
    }
    renderBeverages("loading");
    if (bevLoading) return;
    bevLoading = true;
    fetch("/api/beverages")
      .then(function (res) {
        if (!res.ok) throw new Error("HTTP " + res.status);
        return res.json();
      })
      .then(function (data) {
        bevItems = (data && data.items) || [];
      })
      .catch(function () {
        bevItems = [];
      })
      .then(function () {
        bevLoading = false;
        // Only repaint if the user is still on the 음료 tab.
        if (activeGroup === "음료") renderBeverages("list");
      });
  }

  // Paint the cards for the active group. The fetched list is preserved — we
  // only filter+sort the view (no API re-call). Selection is by name, so it
  // persists across filter/sort changes even for hidden items.
  function renderCards() {
    els.cards.innerHTML = "";

    var q = searchQuery.trim().toLowerCase();
    var filtered;
    if (q) {
      // Search matches by name across the WHOLE list (ignores the chip).
      filtered = allItems.filter(function (item) {
        return String(item.name).toLowerCase().indexOf(q) !== -1;
      });
    } else {
      filtered =
        activeGroup === "전체"
          ? allItems
          : allItems.filter(function (item) {
              return item.group === activeGroup;
            });
    }

    // Sort the full group BEFORE the overview cap so "전체" = first 15 of the
    // fully-sorted list, and a category tab = all of that category, sorted.
    // A search shows ALL matches (the cap only applies to unsearched "전체").
    var sorted = sortItems(filtered);
    var visible =
      !q && activeGroup === "전체" ? sorted.slice(0, OVERVIEW_LIMIT) : sorted;

    if (visible.length === 0) {
      els.feedEmpty.textContent = q
        ? "‘" + searchQuery.trim() + "’ 검색 결과가 없어요"
        : "이 분류는 오늘 추천이 없어요";
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
    // Picking a category clears any active search — search and category are
    // alternative views of the list, not stacked filters.
    if (searchQuery) {
      searchQuery = "";
      els.searchInput.value = "";
      els.searchClear.hidden = true;
    }
    var chips = els.filters.querySelectorAll(".chip");
    chips.forEach(function (chip) {
      var on = chip.getAttribute("data-group") === group;
      chip.classList.toggle("is-active", on);
      chip.setAttribute("aria-pressed", on ? "true" : "false");
    });
    if (group === "음료") {
      // Beverages = NAVER 쇼핑, NOT KAMIS: hide the produce-only UI and
      // render the distinct beverage list. (showBeveragesTab hides
      // recmenu/sorts.)
      showBeveragesTab();
      return;
    }
    // Any other chip → restore the produce UI exactly as before: search box +
    // sorts visible again, recmenu re-shown iff it has picks (renderRecmenu is
    // a pure repaint from selected/top3Names — it does NOT touch selection).
    els.searchbar.hidden = false;
    els.sorts.hidden = false;
    renderRecmenu();
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
    // If the friend arrived via a shared recipe link (?items=…), pre-select
    // those ingredients and auto-open their recipes.
    consumeDeepLink();
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

  // Free-text search: filter the card list by ingredient name as the user
  // types. Empty query restores the active category view.
  function applySearch(value) {
    searchQuery = value;
    els.searchClear.hidden = value.trim() === "";
    renderCards();
  }
  els.searchInput.addEventListener("input", function () {
    applySearch(els.searchInput.value);
  });
  els.searchClear.addEventListener("click", function () {
    els.searchInput.value = "";
    applySearch("");
    els.searchInput.focus();
  });

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

  // ---------------------------------------------------------------------
  // 공유 카드 (viral acquisition): render today's best deals to a branded
  // PNG via <canvas>, then share it with the Web Share API (KakaoTalk etc.
  // on mobile). Desktop / unsupported → download the image + copy the link.
  // Frontend-only, no backend, no keys, no copyright concern (our own data).
  // ---------------------------------------------------------------------
  var _SHARE_URL = "https://today-baguni.onrender.com";
  var _KFONT =
    "-apple-system, 'Apple SD Gothic Neo', 'Malgun Gothic', 'Noto Sans KR', sans-serif";

  function _roundRect(ctx, x, y, w, h, r) {
    ctx.beginPath();
    ctx.moveTo(x + r, y);
    ctx.arcTo(x + w, y, x + w, y + h, r);
    ctx.arcTo(x + w, y + h, x, y + h, r);
    ctx.arcTo(x, y + h, x, y, r);
    ctx.arcTo(x, y, x + w, y, r);
    ctx.closePath();
  }

  function buildShareCanvas(deals) {
    var W = 800,
      H = 800;
    var canvas = document.createElement("canvas");
    canvas.width = W;
    canvas.height = H;
    var ctx = canvas.getContext("2d");

    ctx.fillStyle = "#eaf0ff";
    ctx.fillRect(0, 0, W, H);

    var m = 52;
    _roundRect(ctx, m, m, W - m * 2, H - m * 2, 40);
    ctx.fillStyle = "#ffffff";
    ctx.fill();

    var cx = W / 2;
    ctx.textAlign = "center";

    ctx.fillStyle = "#2f6bff";
    ctx.font = "800 58px " + _KFONT;
    ctx.fillText("장보기 친구", cx, 188);
    ctx.fillStyle = "#9aa3b2";
    ctx.font = "600 28px " + _KFONT;
    ctx.fillText("오늘의 마트 가이드", cx, 234);

    ctx.fillStyle = "#1a1c20";
    ctx.font = "800 42px " + _KFONT;
    ctx.fillText("오늘 평소보다 싸졌어요 🔥", cx, 326);

    var rx1 = m + 70,
      rx2 = W - m - 70,
      y = 432;
    var shown = (deals || []).slice(0, 3);
    if (shown.length === 0) {
      ctx.fillStyle = "#9aa3b2";
      ctx.font = "600 30px " + _KFONT;
      ctx.fillText("오늘의 추천 재료를 확인해보세요", cx, y);
    } else {
      shown.forEach(function (it) {
        ctx.textAlign = "left";
        ctx.fillStyle = "#1a1c20";
        ctx.font = "700 44px " + _KFONT;
        ctx.fillText(it.name, rx1, y);
        if (it.change_pct !== null && it.change_pct !== undefined) {
          ctx.textAlign = "right";
          ctx.fillStyle = "#06a35a";
          ctx.font = "800 44px " + _KFONT;
          ctx.fillText(
            "▼" + Math.abs(Math.round(it.change_pct)) + "%",
            rx2,
            y
          );
        }
        y += 92;
      });
    }

    ctx.textAlign = "center";
    ctx.fillStyle = "#9aa3b2";
    ctx.font = "600 26px " + _KFONT;
    ctx.fillText("today-baguni.onrender.com", cx, H - m - 44);

    return canvas;
  }

  function _flashShareDone(text) {
    if (!els.recmenuShare) return;
    var orig =
      els.recmenuShare.getAttribute("data-label") ||
      els.recmenuShare.textContent;
    els.recmenuShare.setAttribute("data-label", orig);
    els.recmenuShare.textContent = text;
    els.recmenuShare.disabled = true;
    setTimeout(function () {
      els.recmenuShare.textContent = orig;
      els.recmenuShare.disabled = false;
    }, 2200);
  }

  function shareTodayCard() {
    var text =
      "오늘 평소보다 싸진 재료 모음! 🛒 장보기 친구 · 오늘의 마트 가이드\n" +
      _SHARE_URL;
    var deals = top3Names.map(itemByName).filter(function (x) {
      return !!x;
    });
    var canvas = null;
    try {
      canvas = buildShareCanvas(deals);
    } catch (e) {
      canvas = null;
    }
    if (!canvas || !canvas.toBlob) {
      if (navigator.share)
        navigator.share({ title: "장보기 친구", text: text }).catch(function () {});
      return;
    }
    canvas.toBlob(function (blob) {
      var file = blob
        ? new File([blob], "jangboki-chingu.png", { type: "image/png" })
        : null;
      if (file && navigator.canShare && navigator.canShare({ files: [file] })) {
        navigator
          .share({ title: "장보기 친구", text: text, files: [file] })
          .catch(function () {});
        return;
      }
      // Fallback: download the image + copy the link.
      if (blob) {
        var a = document.createElement("a");
        a.href = URL.createObjectURL(blob);
        a.download = "jangboki-chingu.png";
        document.body.appendChild(a);
        a.click();
        document.body.removeChild(a);
        setTimeout(function () {
          URL.revokeObjectURL(a.href);
        }, 1500);
      }
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(_SHARE_URL).catch(function () {});
      }
      _flashShareDone("카드 저장 + 링크 복사됨 ✓");
    }, "image/png");
  }

  // ---- recipe share (selected ingredients → deep-link) ----
  function _flashBtn(btn, text) {
    if (!btn) return;
    var orig = btn.getAttribute("data-label") || btn.textContent;
    btn.setAttribute("data-label", orig);
    btn.textContent = text;
    btn.disabled = true;
    setTimeout(function () {
      btn.textContent = orig;
      btn.disabled = false;
    }, 2200);
  }

  function _recipeDeepLink(names) {
    return _SHARE_URL + "/?items=" + names.map(encodeURIComponent).join(",");
  }

  function buildRecipeShareCanvas(names) {
    var W = 800,
      H = 800;
    var canvas = document.createElement("canvas");
    canvas.width = W;
    canvas.height = H;
    var ctx = canvas.getContext("2d");

    ctx.fillStyle = "#eaf0ff";
    ctx.fillRect(0, 0, W, H);
    var m = 52;
    _roundRect(ctx, m, m, W - m * 2, H - m * 2, 40);
    ctx.fillStyle = "#ffffff";
    ctx.fill();

    var cx = W / 2;
    ctx.textAlign = "center";

    ctx.fillStyle = "#2f6bff";
    ctx.font = "800 58px " + _KFONT;
    ctx.fillText("장보기 친구", cx, 180);
    ctx.fillStyle = "#9aa3b2";
    ctx.font = "600 28px " + _KFONT;
    ctx.fillText("오늘의 마트 가이드", cx, 224);

    ctx.fillStyle = "#1a1c20";
    ctx.font = "800 42px " + _KFONT;
    ctx.fillText("이 재료로 뭐 해먹지? 🍳", cx, 332);

    var label = names.join(" · ");
    var fs = 46;
    ctx.fillStyle = "#06a35a";
    ctx.font = "800 " + fs + "px " + _KFONT;
    while (fs > 24 && ctx.measureText(label).width > W - m * 2 - 70) {
      fs -= 2;
      ctx.font = "800 " + fs + "px " + _KFONT;
    }
    ctx.fillText(label, cx, 452);

    ctx.fillStyle = "#1a1c20";
    ctx.font = "700 34px " + _KFONT;
    ctx.fillText("레시피 보러가기 →", cx, 562);

    ctx.fillStyle = "#9aa3b2";
    ctx.font = "600 26px " + _KFONT;
    ctx.fillText("today-baguni.onrender.com", cx, H - m - 44);

    return canvas;
  }

  function shareRecipeCard(names, btn) {
    names = (names || []).filter(function (n) {
      return !!n;
    });
    if (!names.length) return;
    var deepUrl = _recipeDeepLink(names);
    var text =
      "내가 고른 재료(" +
      names.join(", ") +
      ")로 만들 레시피! 🍳 장보기 친구\n" +
      deepUrl;
    var canvas = null;
    try {
      canvas = buildRecipeShareCanvas(names);
    } catch (e) {
      canvas = null;
    }
    if (!canvas || !canvas.toBlob) {
      if (navigator.share)
        navigator.share({ title: "장보기 친구", text: text }).catch(function () {});
      return;
    }
    canvas.toBlob(function (blob) {
      var file = blob
        ? new File([blob], "jangboki-recipe.png", { type: "image/png" })
        : null;
      if (file && navigator.canShare && navigator.canShare({ files: [file] })) {
        navigator
          .share({ title: "장보기 친구", text: text, files: [file] })
          .catch(function () {});
        return;
      }
      if (blob) {
        var a = document.createElement("a");
        a.href = URL.createObjectURL(blob);
        a.download = "jangboki-recipe.png";
        document.body.appendChild(a);
        a.click();
        document.body.removeChild(a);
        setTimeout(function () {
          URL.revokeObjectURL(a.href);
        }, 1500);
      }
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(deepUrl).catch(function () {});
      }
      _flashBtn(btn, "카드 저장 + 링크 복사됨 ✓");
    }, "image/png");
  }

  // Friend opened a shared recipe link (?items=A,B): pre-select those
  // ingredients and auto-open the recipe sheet so they see the same recipes.
  var _deepLinkDone = false;
  function consumeDeepLink() {
    if (_deepLinkDone) return;
    _deepLinkDone = true;
    var q = null;
    try {
      q = new URLSearchParams(window.location.search).get("items");
    } catch (e) {
      q = null;
    }
    if (!q) return;
    var names = q
      .split(",")
      .map(function (s) {
        return s.trim();
      })
      .filter(function (s) {
        return s.length > 0;
      });
    if (!names.length) return;
    // clean the URL so a refresh / back doesn't re-open the sheet
    try {
      window.history.replaceState({}, "", window.location.pathname);
    } catch (e) {}
    selected = names.slice();
    syncSelectionUI();
    openComboFor(selected);
  }

  els.recmenuGo.addEventListener("click", function () {
    // Same path as the action bar — recipes for the one shared basket.
    openComboFor(selected);
  });

  if (els.recmenuShare) {
    els.recmenuShare.addEventListener("click", shareTodayCard);
  }

  // ---------------------------------------------------------------------
  // 오늘의 꿀팁 (date-seeded daily rotation) + "오늘 뭐 먹지?" 룰렛.
  // Light engagement, fully client-side. Tips are a curated standalone
  // list (no data dependency); the roulette spins through today's items.
  // ---------------------------------------------------------------------
  var _DAILY_TIPS = [
    "양파는 망에 담아 바람 통하는 어두운 곳에 두면 한 달은 거뜬해요",
    "대파는 쫑쫑 썰어 냉동하면 필요할 때 바로 꺼내 써요",
    "감자와 양파는 같이 두면 빨리 상해요 — 따로 보관하세요",
    "고기는 1회분씩 나눠 냉동하면 해동도 빠르고 안 버려요",
    "시금치·나물은 데쳐서 소분 냉동하면 오래 가요",
    "계란은 뾰족한 쪽이 아래로 가게 두면 더 오래 신선해요",
    "다진 마늘은 큐브 틀에 얼려두면 쓰기 편해요",
    "빵은 냉장 말고 냉동 — 먹을 만큼 꺼내 데우면 갓 구운 맛",
    "상추·잎채소는 물기 닦아 키친타월에 싸서 냉장하세요",
    "버섯은 씻지 말고 봉지째 냉장, 쓰기 직전에 털어내세요",
    "생강은 껍질째 냉동해두고 필요할 때 갈아 쓰면 안 버려요",
    "토마토는 냉장하면 맛이 떨어져요 — 실온에 두고 빨리 드세요",
    "자취엔 '소분'이 핵심 — 사오면 바로 1회분씩 나눠두기",
    "두부 남으면 물에 잠기게 담아 냉장, 물 자주 갈면 며칠 가요",
    "우유는 유통기한 임박하면 얼려도 OK (해동 후 요리용)",
    "멸치는 냉동 보관해야 눅눅함·비린내가 안 생겨요",
    "바나나는 꼭지를 랩으로 감으면 갈변이 늦어져요",
    "마른 김은 지퍼백에 실리카겔과 함께 두면 눅눅해지지 않아요",
  ];

  function setDailyTip() {
    if (!els.dailyTip) return;
    var d = new Date();
    var seed = d.getFullYear() * 372 + d.getMonth() * 31 + d.getDate();
    els.dailyTip.textContent = "💡 " + _DAILY_TIPS[seed % _DAILY_TIPS.length];
  }

  var _ROLL_STAPLES = { 양파: 1, 대파: 1, 마늘: 1, 생강: 1 };
  var _rollPick = null;
  var _rollTimer = null;
  var _rollStopT = null;
  var _rollSpinning = false;

  function _rollNames() {
    var pool = allItems.filter(function (it) {
      return !_ROLL_STAPLES[it.name];
    });
    if (pool.length < 3) pool = allItems;
    return pool.map(function (it) {
      return it.name;
    });
  }

  // Slot-machine style: spin keeps going until the user taps 그만 (or a 7s
  // safety auto-stop). 레시피 보기 only works once stopped.
  function _startSpin() {
    var names = _rollNames();
    if (!names.length) return;
    if (_rollTimer) clearInterval(_rollTimer);
    if (_rollStopT) clearTimeout(_rollStopT);
    _rollSpinning = true;
    _rollPick = null;
    if (els.rollAgain) els.rollAgain.textContent = "✋ 그만";
    if (els.rollRecipe) els.rollRecipe.disabled = true;
    _rollTimer = setInterval(function () {
      els.rollPick.textContent =
        names[Math.floor(Math.random() * names.length)];
    }, 60);
    _rollStopT = setTimeout(function () {
      if (_rollSpinning) _stopSpin();
    }, 7000);
  }

  function _stopSpin() {
    if (_rollTimer) {
      clearInterval(_rollTimer);
      _rollTimer = null;
    }
    if (_rollStopT) {
      clearTimeout(_rollStopT);
      _rollStopT = null;
    }
    if (!_rollSpinning) return;
    _rollSpinning = false;
    _rollPick = els.rollPick.textContent;
    els.rollPick.classList.add("is-final");
    setTimeout(function () {
      if (els.rollPick) els.rollPick.classList.remove("is-final");
    }, 450);
    if (els.rollAgain) els.rollAgain.textContent = "🎲 다시";
    if (els.rollRecipe) els.rollRecipe.disabled = false;
  }

  function _toggleSpin() {
    if (_rollSpinning) _stopSpin();
    else _startSpin();
  }

  function openRoulette() {
    if (!allItems.length || !els.rollPop) return;
    els.rollPop.hidden = false;
    if (els.rollPick) els.rollPick.textContent = "?";
    _startSpin();
  }

  function closeRoulette() {
    if (_rollTimer) {
      clearInterval(_rollTimer);
      _rollTimer = null;
    }
    if (_rollStopT) {
      clearTimeout(_rollStopT);
      _rollStopT = null;
    }
    _rollSpinning = false;
    if (els.rollPop) els.rollPop.hidden = true;
  }

  function rollToRecipe() {
    if (_rollSpinning || !_rollPick) return;
    closeRoulette();
    openComboFor([_rollPick]);
  }

  if (els.rollBtn) els.rollBtn.addEventListener("click", openRoulette);
  if (els.rollAgain) els.rollAgain.addEventListener("click", _toggleSpin);
  if (els.rollRecipe) els.rollRecipe.addEventListener("click", rollToRecipe);
  if (els.rollClose) els.rollClose.addEventListener("click", closeRoulette);
  if (els.rollBackdrop)
    els.rollBackdrop.addEventListener("click", closeRoulette);

  els.actionbarClear.addEventListener("click", clearSelection);
  els.actionbarGo.addEventListener("click", openCombo);
  els.comboClose.addEventListener("click", closeSheet);
  els.comboBackdrop.addEventListener("click", closeSheet);

  // ---------------------------------------------------------------------
  // PWA "한 번 더 누르면 종료" back guard. ONLY in installed/standalone mode
  // — in a normal browser tab Back must keep its native meaning, so we do
  // NOT hijack it there. Mechanism: a History sentinel + popstate. Back
  // while the recipe sheet is open just closes the sheet. At the app root,
  // the first Back shows a brief toast and is cancelled; a second Back
  // within 2s actually leaves (closes the installed app). iOS standalone
  // has no system Back key, so this is effectively an Android pattern
  // (harmless no-op elsewhere).
  // ---------------------------------------------------------------------
  function isStandalone() {
    try {
      return (
        (window.matchMedia &&
          window.matchMedia("(display-mode: standalone)").matches) ||
        window.navigator.standalone === true
      );
    } catch (e) {
      return false;
    }
  }

  var exitToastTimer = null;

  function showExitToast() {
    if (!els.exitToast) return;
    els.exitToast.hidden = false;
    // Force a reflow so the fade-in transition runs even on re-show.
    void els.exitToast.offsetWidth;
    els.exitToast.classList.add("is-on");
  }

  function hideExitToast() {
    if (!els.exitToast) return;
    els.exitToast.classList.remove("is-on");
    setTimeout(function () {
      if (els.exitToast && !els.exitToast.classList.contains("is-on")) {
        els.exitToast.hidden = true;
      }
    }, 220);
  }

  function setupBackGuard() {
    if (!isStandalone()) return;
    var armed = false;

    function pushSentinel() {
      try {
        history.pushState({ _baguni_guard: 1 }, "");
      } catch (e) {}
    }

    // Seed one sentinel so the first Back lands in popstate (not "leave").
    pushSentinel();

    window.addEventListener("popstate", function () {
      // 1) Back closes an open recipe sheet first.
      if (els.comboSheet && !els.comboSheet.hidden) {
        closeSheet();
        pushSentinel();
        return;
      }
      // 2) App root: 1st Back = warn + cancel; 2nd Back (≤2s) = leave.
      if (!armed) {
        armed = true;
        showExitToast();
        pushSentinel(); // cancel this Back — stay in the app
        if (exitToastTimer) clearTimeout(exitToastTimer);
        exitToastTimer = setTimeout(function () {
          armed = false;
          hideExitToast();
        }, 2000);
        return;
      }
      // armed && within 2s: let it leave. We did NOT re-push; step back
      // once more past the original entry → installed PWA closes.
      if (exitToastTimer) clearTimeout(exitToastTimer);
      hideExitToast();
      setTimeout(function () {
        try {
          history.back();
        } catch (e) {}
      }, 0);
    });
  }

  setupBackGuard();
  load();
})();
