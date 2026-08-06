// newsprism dashboard — client-side interactions.
// Extracted from newsprism.py; each IIFE self-guards on the DOM elements
// it needs, so blocks whose elements are absent simply do nothing.

// ---- filter ----
(function(){
  var REST = '\u00ffrest';
  var chips = document.querySelectorAll('.chip');
  var cards = document.querySelectorAll('#cluster-list .card');
  function activeFilter(){
    var a = document.querySelector('.chip.active');
    return a ? a.getAttribute('data-filter') : '*';
  }
  function applyFilter(){
    var f = activeFilter();
    // keyword terms come from the shared search box (if present); a card must
    // match the active hotspot AND contain every term (AND, substring).
    var terms = (window.__npSearchTerms || []);
    cards.forEach(function(card){
      var okHot = (f === '*' || card.getAttribute('data-hotspot') === f);
      var okKw = true;
      if (terms.length){
        var blob = card.getAttribute('data-search') || '';
        for (var i = 0; i < terms.length; i++){
          if (blob.indexOf(terms[i]) === -1){ okKw = false; break; }
        }
      }
      card.style.display = (okHot && okKw) ? '' : 'none';
    });
  }
  window.__npApplyFilter = applyFilter;   // let the search box trigger it too
  chips.forEach(function(chip){
    chip.addEventListener('click', function(){
      chips.forEach(function(c){ c.classList.remove('active'); });
      chip.classList.add('active');
      applyFilter();
    });
  });
  // Remove a single cluster from its hotspot grouping (client-side only;
  // resets on the next generated page). The card stays in the flat list under
  // "Weitere"; only its hotspot assignment is dropped and the chip counts and
  // the size number on the matching chip are updated.
  function sizeOf(card){
    var s = card.querySelector('.size');
    var n = s ? parseInt(s.textContent, 10) : 0;
    return isNaN(n) ? 0 : n;
  }
  function adjustChip(filterVal, delta){
    var chip = document.querySelector('.chip[data-filter="' + (window.CSS && CSS.escape ? CSS.escape(filterVal) : filterVal) + '"]');
    if (!chip) return;
    var nEl = chip.querySelector('.chip-n');
    if (nEl){
      var v = parseInt(nEl.textContent, 10) || 0;
      v += delta;
      if (v <= 0){ chip.remove(); }   // hotspot empty -> drop the chip
      else { nEl.textContent = v; }
    }
  }
  document.addEventListener('click', function(ev){
    var btn = ev.target.closest ? ev.target.closest('.hs-remove') : null;
    if (!btn) return;
    ev.preventDefault();
    ev.stopPropagation();
    var card = btn.closest('.card');
    if (!card) return;
    var old = card.getAttribute('data-hotspot');
    if (!old || old === REST) return;
    var sz = sizeOf(card);
    // move the card into the "rest" bucket
    card.setAttribute('data-hotspot', REST);
    var tag = btn.closest('.hs-tag');
    if (tag) tag.remove();
    // update chip counts: shrink the old hotspot, grow "Weitere"
    adjustChip(old, -sz);
    var restChip = document.querySelector('.chip[data-filter="' + REST + '"]');
    if (restChip){ adjustChip(REST, sz); }
    // if the old hotspot was the active filter, this card now hides itself
    applyFilter();
  });
})();

// ---- bs ----
(function(){
  var box = document.getElementById('bs-box');
  if (!box) return;
  var countEl = document.getElementById('bs-count');
  function esc(v){ return (window.CSS && CSS.escape) ? CSS.escape(v) : v; }
  document.addEventListener('click', function(ev){
    var btn = ev.target.closest ? ev.target.closest('.bs-remove') : null;
    if (!btn) return;
    ev.preventDefault();
    ev.stopPropagation();
    var card = btn.closest('.card');
    if (!card || !box.contains(card)) return;   // only act on the box copy
    var cid = card.getAttribute('data-cid');
    // remove the blindspot badge on the same cluster's copy in the flat list
    if (cid){
      var listCard = document.querySelector('#cluster-list .card[data-cid="' + esc(cid) + '"]');
      if (listCard){
        var b = listCard.querySelector('.bs');
        if (b) b.remove();
      }
    }
    card.remove();
    var left = box.querySelectorAll('.card').length;
    if (countEl){ countEl.textContent = '(' + left + ')'; }
    if (left === 0){ box.style.display = 'none'; }
  });
})();

// ---- color ----
(function(){
  var btn = document.getElementById('color-toggle');
  var nameEl = document.getElementById('color-scheme-name');
  if (!btn) return;
  btn.addEventListener('click', function(){
    var us = document.body.classList.toggle('us-colors');
    if (nameEl) nameEl.textContent = us ? 'US' : 'EU';
  });
})();

// ---- search ----
(function(){
  var box = document.getElementById('kw-search');
  if (!box) return;
  var cards = document.querySelectorAll('#cluster-list .card');
  var countEl = document.getElementById('kw-count');
  var bsBox = document.getElementById('bs-box');
  var bsCount = document.getElementById('bs-count');
  window.__npSearchTerms = [];
  function matches(card, terms){
    if (!terms.length) return true;
    var blob = card.getAttribute('data-search') || '';
    for (var i = 0; i < terms.length; i++){
      if (blob.indexOf(terms[i]) === -1) return false;
    }
    return true;
  }
  // --- highlighting (safe, text-node based - never innerHTML on user text) ---
  // Selectors of the text-bearing parts that are also the search corpus:
  // the label, the source links, and the article-list items.
  var HL_SEL = '.label, .srcs a, .arts li';
  function clearHL(card){
    var marks = card.querySelectorAll('mark.kw-hit');
    marks.forEach(function(m){
      var p = m.parentNode;
      p.replaceChild(document.createTextNode(m.textContent), m);
      p.normalize();   // merge adjacent text nodes back
    });
  }
  function hlTextNode(node, terms){
    var text = node.nodeValue;
    var low = text.toLowerCase();
    // find the earliest match of any term at each position
    var hit = -1, hitLen = 0, hitPos = text.length;
    for (var i = 0; i < terms.length; i++){
      var p = low.indexOf(terms[i]);
      if (p !== -1 && p < hitPos){ hitPos = p; hitLen = terms[i].length; hit = p; }
    }
    if (hit === -1) return 0;
    var frag = document.createDocumentFragment();
    var count = 0, idx = 0;
    while (true){
      // earliest term match at/after idx
      var best = -1, bestLen = 0;
      for (var j = 0; j < terms.length; j++){
        var pp = low.indexOf(terms[j], idx);
        if (pp !== -1 && (best === -1 || pp < best)){ best = pp; bestLen = terms[j].length; }
      }
      if (best === -1){ frag.appendChild(document.createTextNode(text.slice(idx))); break; }
      if (best > idx) frag.appendChild(document.createTextNode(text.slice(idx, best)));
      var m = document.createElement('mark');
      m.className = 'kw-hit';
      m.textContent = text.slice(best, best + bestLen);
      frag.appendChild(m);
      count++;
      idx = best + bestLen;
    }
    node.parentNode.replaceChild(frag, node);
    return count;
  }
  function highlight(card, terms){
    clearHL(card);
    if (!terms.length) return 0;
    var total = 0;
    card.querySelectorAll(HL_SEL).forEach(function(el){
      // only direct text nodes of el (skip nested elements already handled)
      var kids = Array.prototype.slice.call(el.childNodes);
      kids.forEach(function(n){
        if (n.nodeType === 3) total += hlTextNode(n, terms);
      });
    });
    return total;
  }
  function setHits(card, n){
    var el = card.querySelector('.kw-hits');
    if (el) el.textContent = n ? (' \u00b7 ' + n + ' Treffer') : '';
  }
  function fallbackApply(){
    // used only when there is no hotspot filter on the page
    var terms = window.__npSearchTerms;
    cards.forEach(function(card){
      card.style.display = matches(card, terms) ? '' : 'none';
    });
  }
  function applyToBox(){
    // also filter the highlighted blindspots box: matching cards stay, the
    // box counter follows, and the whole box hides when nothing matches.
    if (!bsBox) return;
    var terms = window.__npSearchTerms;
    var bsCards = bsBox.querySelectorAll('.card');
    var vis = 0;
    bsCards.forEach(function(card){
      var ok = matches(card, terms);
      card.style.display = ok ? '' : 'none';
      if (ok) vis++;
    });
    if (bsCount) bsCount.textContent = '(' + vis + ')';
    bsBox.style.display = (vis === 0) ? 'none' : '';
  }
  function run(){
    var q = box.value.toLowerCase().trim();
    window.__npSearchTerms = q ? q.split(/\s+/) : [];
    var terms = window.__npSearchTerms;
    // filtering is cheap -> do it immediately for responsive show/hide
    if (window.__npApplyFilter){ window.__npApplyFilter(); }
    else { fallbackApply(); }
    applyToBox();
    if (countEl){
      if (terms.length){
        var vis = 0;
        cards.forEach(function(c){ if (c.style.display !== 'none') vis++; });
        countEl.textContent = vis + ' Treffer';
      } else {
        countEl.textContent = '';
      }
    }
    // highlighting touches many DOM nodes (large clusters have hundreds of
    // list items) -> debounce it so fast typing doesn't stutter.
    if (window.__npHlTimer) clearTimeout(window.__npHlTimer);
    window.__npHlTimer = setTimeout(function(){
      document.querySelectorAll('.card').forEach(function(card){
        if (terms.length && card.style.display !== 'none'){
          setHits(card, highlight(card, terms));
        } else {
          clearHL(card); setHits(card, 0);
        }
      });
    }, 160);
  }
  box.addEventListener('input', run);
})();

// ---- share ----
(function(){
  function mailLink(title, text){
    return 'mailto:?subject=' + encodeURIComponent(title) +
           '&body=' + encodeURIComponent(text);
  }
  function copyText(btn, text){
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(function(){
        var old = btn.textContent;
        btn.textContent = 'kopiert';
        btn.classList.add('copied');
        setTimeout(function(){ btn.textContent = old; btn.classList.remove('copied'); }, 1500);
      }).catch(function(){
        window.prompt('Zum Kopieren markieren und Strg+C:', text);
      });
    } else {
      window.prompt('Zum Kopieren markieren und Strg+C:', text);
    }
  }
  var openMenu = null;
  function closeMenu(){ if (openMenu){ openMenu.remove(); openMenu = null; } }
  document.addEventListener('click', function(){ closeMenu(); });

  function showMenu(btn, title, text){
    closeMenu();
    var menu = document.createElement('div');
    menu.className = 'share-menu';
    var bCopy = document.createElement('button');
    bCopy.type = 'button'; bCopy.textContent = 'Kopieren';
    bCopy.addEventListener('click', function(e){ e.stopPropagation(); closeMenu(); copyText(btn, text); });
    var aMail = document.createElement('a');
    aMail.textContent = 'E-Mail'; aMail.href = mailLink(title, text);
    aMail.addEventListener('click', function(e){ e.stopPropagation(); closeMenu(); });
    menu.appendChild(bCopy); menu.appendChild(aMail);
    btn.parentNode.insertBefore(menu, btn.nextSibling);
    openMenu = menu;
  }

  document.querySelectorAll('.share').forEach(function(btn){
    btn.addEventListener('click', function(e){
      e.stopPropagation();
      var text = btn.getAttribute('data-share') || '';
      var title = btn.getAttribute('data-title') || 'NewsPrism';
      if (navigator.share) {                 // mobil: direkt Share-Sheet
        navigator.share({title: title, text: text}).catch(function(){});
        return;
      }
      showMenu(btn, title, text);            // desktop: selection menu
    });
  });
})();

// ---- refresh ----
(function(){
  var btn = document.getElementById('refresh-btn');
  var msg = document.getElementById('refresh-msg');
  if(!btn) return;
  btn.addEventListener('click', function(){
    btn.disabled = true; msg.textContent = ' wird angestoßen ...';
    msg.style.color = '';
    fetch('refresh', {method:'POST'}).then(function(r){
      return r.text().then(function(t){
        msg.textContent = ' ' + t;
        // 202 = triggered (success), 429 = already running / cooldown (notice)
        msg.style.color = (r.status === 202) ? '#15803d' : '#b5341f';
        // keep the button disabled on 202 (run in progress), otherwise re-enable
        if(r.status !== 202){ setTimeout(function(){ btn.disabled = false; }, 3000); }
      });
    }).catch(function(){
      msg.textContent = ' Fehler beim Anstoßen.';
      msg.style.color = '#b5341f';
      btn.disabled = false;
    });
  });
})();
