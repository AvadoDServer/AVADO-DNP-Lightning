// Draws the page model from /api/status. Shared by the AVADO Bitcoin packages
// (Bitcoin node, Electrum server, Bitcoin explorer, Lightning). This copy
// (Lightning) adds checklists, forms, download links, "reveal" cards (secrets
// shown only after a button press: the 24 words, the phone QR code) and the
// page PIN (asked when the server answers 401 with "pin", then the action is
// repeated), and is
// otherwise identical, so it can replace the others when they next change.
// Everything is inserted as text (never as HTML), so nothing the server or a
// peer sends can run as code on this page. URLs are relative, so the page also
// works under a path (the explorer serves it at /avado/).
"use strict";

(function () {
  var main = document.getElementById("main");
  var timer = null;
  var busy = false;
  var scrolled = false;
  var asking = null; // an in-page "are you sure?" that is open (kept until answered)
  var reveal = null; // a card an action asked to show (kept across refreshes until closed)
  var lastModel = null;
  var revealScroll = false;

  function el(tag, cls, text) {
    var node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  }

  function paragraphs(parent, text, cls) {
    if (!text) return;
    var list = Array.isArray(text) ? text : [text];
    list.forEach(function (t) {
      if (t) parent.appendChild(el("p", cls, t));
    });
  }

  function toast(message) {
    var old = document.querySelector(".toast");
    if (old) old.remove();
    var node = el("div", "toast", message);
    node.setAttribute("role", "status");
    document.body.appendChild(node);
    setTimeout(function () {
      node.remove();
    }, 6000);
  }

  function copyText(text, button) {
    function done() {
      var label = button.textContent;
      button.textContent = "Copied";
      setTimeout(function () {
        button.textContent = label;
      }, 1500);
    }
    // navigator.clipboard needs https; my.ava.do pages are plain http, so fall
    // back to a hidden text field.
    if (navigator.clipboard && window.isSecureContext) {
      navigator.clipboard.writeText(text).then(done, function () {});
      return;
    }
    var area = el("textarea");
    area.value = text;
    area.setAttribute("readonly", "");
    area.style.position = "fixed";
    area.style.opacity = "0";
    document.body.appendChild(area);
    area.select();
    try {
      document.execCommand("copy");
      done();
    } catch (e) {
      /* the text is still selectable on the page */
    }
    area.remove();
  }

  // Asks "are you sure?" on the page, under the button. No browser pop-ups
  // (confirm/alert/prompt): they can freeze the browser, and Enter pressed
  // them. Cancel has the focus, so Enter or Escape cancels.
  function askConfirm(button, spec) {
    closeConfirm();
    var row = button.closest(".btns") || button.parentNode;
    var box = el("div", "confirm");
    box.setAttribute("role", "alertdialog");
    box.setAttribute("aria-label", spec.label || "Are you sure?");
    var text = el("p", null, spec.confirm);
    text.id = "confirm-text";
    box.setAttribute("aria-describedby", text.id);
    box.appendChild(text);
    var btns = el("div", "btns");
    var label = spec.label ? spec.label.charAt(0).toLowerCase() + spec.label.slice(1) : "continue";
    var yes = el("button", spec.style === "danger" ? "danger" : "primary", spec.confirmLabel || "Yes, " + label);
    yes.type = "button";
    var no = el("button", null, "Cancel");
    no.type = "button";
    yes.addEventListener("click", function () {
      closeConfirm();
      runAction(button, spec, true);
    });
    no.addEventListener("click", function () {
      closeConfirm();
      if (document.body.contains(button)) button.focus();
    });
    box.addEventListener("keydown", function (ev) {
      if (ev.key === "Escape") no.click();
    });
    btns.appendChild(yes);
    btns.appendChild(no);
    box.appendChild(btns);
    row.parentNode.insertBefore(box, row.nextSibling);
    row.hidden = true;
    asking = { box: box, row: row };
    no.focus();
    box.scrollIntoView({ block: "nearest" });
  }

  function closeConfirm() {
    if (!asking) return;
    asking.box.remove();
    asking.row.hidden = false;
    asking = null;
  }

  function runAction(button, spec, confirmed) {
    if (busy) return;
    if (spec.confirm && !confirmed) {
      askConfirm(button, spec);
      return;
    }
    busy = true;
    button.disabled = true;
    fetch("api/action/" + encodeURIComponent(spec.action), {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Avado": "1" },
      body: JSON.stringify(spec.body || {}),
      cache: "no-store",
    })
      .then(function (resp) {
        return resp.json().then(function (data) {
          return { ok: resp.ok, status: resp.status, data: data };
        });
      })
      .then(function (r) {
        if (r.status === 401 && r.data.pin) {
          askPin(r.data.pin, { action: spec }, r.data.error);
          return;
        }
        if (r.ok && r.data.reveal) {
          reveal = r.data.reveal;
          revealScroll = true;
        } else if (r.ok && reveal && spec.closesReveal !== false) {
          reveal = null; // e.g. "I have written them down"
        }
        if (r.ok && spec.form) spec.form.reset();
        if (r.ok && r.data.reveal && !r.data.message) return;
        toast(r.ok ? r.data.message || "Done." : r.data.error || "That did not work. Please try again.");
      })
      .catch(function () {
        toast("The app did not answer. Please try again in a minute.");
      })
      .then(function () {
        busy = false;
        button.disabled = false;
        load();
      });
  }

  // Links and action buttons in one row ("btns"); used by cards and by "Details" boxes.
  function renderButtons(parent, links, buttons) {
    if (!(links && links.length) && !(buttons && buttons.length)) return;
    var btns = el("div", "btns");
    (links || []).forEach(function (l) {
      var a = el("a", "btn" + (l.style ? " " + l.style : ""), l.label);
      a.href = l.href;
      if (l.download) {
        a.setAttribute("download", "");
        a.addEventListener("click", function (ev) {
          ev.preventDefault();
          download(l.href);
        });
      }
      if (/^https?:/.test(l.href) && l.external) {
        a.target = "_blank";
        a.rel = "noopener noreferrer";
      }
      btns.appendChild(a);
    });
    (buttons || []).forEach(function (spec) {
      var b = el("button", spec.style || null, spec.label);
      b.type = "button";
      b.addEventListener("click", function () {
        runAction(b, spec);
      });
      btns.appendChild(b);
    });
    parent.appendChild(btns);
  }

  function renderHero(model) {
    var card = el("section", "card hero state-" + (model.state || "working"));
    card.appendChild(el("span", "dot"));
    var body = el("div", "hero-body");
    body.appendChild(el("h1", null, model.headline || ""));
    paragraphs(body, model.text, "muted");
    if (model.progress && typeof model.progress.percent === "number") {
      var bar = el("div", "bar");
      bar.setAttribute("role", "progressbar");
      bar.setAttribute("aria-valuemin", "0");
      bar.setAttribute("aria-valuemax", "100");
      var pct = Math.max(0, Math.min(100, model.progress.percent));
      bar.setAttribute("aria-valuenow", pct.toFixed(1));
      var fill = el("span");
      fill.style.width = pct.toFixed(2) + "%";
      bar.appendChild(fill);
      body.appendChild(bar);
      var label = el("div", "bar-label");
      label.appendChild(el("span", null, model.progress.left || pct.toFixed(1) + "%"));
      if (model.progress.right) label.appendChild(el("span", null, model.progress.right));
      body.appendChild(label);
    }
    card.appendChild(body);
    return card;
  }

  function renderFacts(facts) {
    var grid = el("section", "facts");
    facts.forEach(function (f) {
      var box = el("div", "fact" + (f.tone ? " tone-" + f.tone : ""));
      box.appendChild(el("div", "k", f.k));
      box.appendChild(el("div", "v", f.v));
      grid.appendChild(box);
    });
    return grid;
  }

  function renderSection(s) {
    var card = el("section", "card" + (s.tone ? " tone-" + s.tone : ""));
    if (s.id) card.id = s.id;
    if (s.title) card.appendChild(el("h2", null, s.title));
    paragraphs(card, s.text);
    // Values first: the steps below refer to them ("the address and port above").
    if (s.fields && s.fields.length) {
      var fields = el("div", "fields");
      s.fields.forEach(function (f) {
        var row = el("div", "field");
        row.appendChild(el("span", "k", f.k));
        row.appendChild(el("span", "v", f.v));
        if (f.copy) {
          var b = el("button", "small", "Copy");
          b.type = "button";
          b.addEventListener("click", function () {
            copyText(String(f.v), b);
          });
          row.appendChild(b);
        }
        fields.appendChild(row);
      });
      card.appendChild(fields);
    }
    paragraphs(card, s.howto);
    if (s.steps && s.steps.length) {
      var ol = el("ol", "steps");
      s.steps.forEach(function (t) {
        ol.appendChild(el("li", null, t));
      });
      card.appendChild(ol);
    }
    if (s.checklist && s.checklist.length) card.appendChild(renderChecklist(s.checklist));
    paragraphs(card, s.after, "muted");
    renderButtons(card, s.links, s.buttons);
    // "Details": extra steps, notes and buttons ("more"), then a table ("advanced"), folded away.
    var more = s.more || [];
    var adv = s.advanced || [];
    if (more.length || adv.length) {
      var det = el("details", "adv");
      det.appendChild(el("summary", null, s.advancedTitle || "Details"));
      more.forEach(function (m) {
        var box = el("div", "more");
        if (m.title) box.appendChild(el("h3", null, m.title));
        paragraphs(box, m.text);
        if (m.steps && m.steps.length) {
          var list = el("ol", "steps");
          m.steps.forEach(function (t) {
            list.appendChild(el("li", null, t));
          });
          box.appendChild(list);
        }
        renderButtons(box, m.links, m.buttons);
        if (m.form) box.appendChild(renderForm(m.form));
        det.appendChild(box);
      });
      if (adv.length) {
        var table = el("table", "kv");
        adv.forEach(function (row) {
          var tr = el("tr");
          tr.appendChild(el("td", null, row.k));
          tr.appendChild(el("td", null, row.v));
          table.appendChild(tr);
        });
        det.appendChild(table);
      }
      card.appendChild(det);
    }
    return card;
  }

  // Fetches a file (the PIN may be asked first) and hands it to the browser to save.
  function download(href) {
    fetch(href, { cache: "no-store" })
      .then(function (resp) {
        if (resp.ok) {
          var name = /filename="([^"]+)"/.exec(resp.headers.get("Content-Disposition") || "");
          return resp.blob().then(function (blob) {
            var url = URL.createObjectURL(blob);
            var a = el("a");
            a.href = url;
            a.download = name ? name[1] : "download";
            document.body.appendChild(a);
            a.click();
            a.remove();
            setTimeout(function () {
              URL.revokeObjectURL(url);
            }, 10000);
            setTimeout(load, 1500); // the step ticks once the file was fetched
          });
        }
        return resp.text().then(function (text) {
          var data = {};
          try {
            data = JSON.parse(text);
          } catch (e) {
            data = { error: text };
          }
          if (resp.status === 401 && data.pin) askPin(data.pin, { download: href }, data.error);
          else toast(data.error || "That did not work. Please try again.");
        });
      })
      .catch(function () {
        toast("The app did not answer. Please try again in a minute.");
      });
  }

  // The page PIN: choose one ("set") or enter it ("enter"), then repeat what was asked.
  function askPin(need, retry, message) {
    reveal = { pin: need, retry: retry, message: message };
    revealScroll = true;
    if (lastModel) render(lastModel);
  }

  function renderPin(r) {
    var card = el("section", "card reveal");
    card.id = "pin";
    var set = r.pin === "set";
    card.appendChild(el("h2", null, set ? "Choose a PIN for this page" : "Enter your PIN"));
    paragraphs(card, set
      ? "It protects your 24 words, the phone code and the backup. Forgot it? Restart Lightning in the AVADO Admin."
      : r.message && r.message !== "Enter your PIN." ? r.message : "The PIN you chose for this page.", "muted");
    var form = el("form", "form");
    function field(label) {
      var l = el("label", null, label);
      var i = el("input");
      i.type = "password";
      i.setAttribute("inputmode", "numeric");
      i.setAttribute("autocomplete", "off");
      i.setAttribute("maxlength", "12");
      l.appendChild(i);
      form.appendChild(l);
      return i;
    }
    var first = field(set ? "PIN (6 to 12 digits)" : "PIN");
    var again = set ? field("The same PIN again") : null;
    var btns = el("div", "btns");
    var ok = el("button", "primary", set ? "Save PIN" : "Unlock");
    ok.type = "submit";
    var cancel = el("button", null, "Cancel");
    cancel.type = "button";
    cancel.addEventListener("click", function () {
      reveal = null;
      if (lastModel) render(lastModel);
    });
    btns.appendChild(ok);
    btns.appendChild(cancel);
    form.appendChild(btns);
    form.addEventListener("submit", function (ev) {
      ev.preventDefault();
      ok.disabled = true;
      var body = set ? { pin: first.value, again: again.value } : { pin: first.value };
      fetch("api/action/" + (set ? "pin-set" : "pin-enter"), {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-Avado": "1" },
        body: JSON.stringify(body),
        cache: "no-store",
      })
        .then(function (resp) {
          return resp.json().then(function (data) {
            return { ok: resp.ok, data: data };
          });
        })
        .then(function (res) {
          ok.disabled = false;
          if (!res.ok) {
            form.reset();
            toast(res.data.error || "That did not work. Please try again.");
            return;
          }
          var retry = r.retry || {};
          reveal = null;
          if (lastModel) render(lastModel);
          if (retry.download) download(retry.download);
          else if (retry.action) runAction(el("button"), Object.assign({}, retry.action, { confirm: null }));
        })
        .catch(function () {
          ok.disabled = false;
          toast("The app did not answer. Please try again in a minute.");
        });
    });
    card.appendChild(form);
    if (!r.focused) {
      r.focused = true; // once, not at every refresh
      setTimeout(function () {
        first.focus();
      }, 50);
    }
    return card;
  }

  // Numbered set-up steps with a live tick.
  function renderChecklist(items) {
    var ol = el("ol", "checklist");
    items.forEach(function (item, i) {
      var li = el("li", item.done ? "done" : null);
      li.appendChild(el("span", "tick", item.done ? "\u2713" : String(i + 1)));
      var body = el("div", "check-body");
      body.appendChild(el("div", "check-title", item.title));
      if (!item.done) {
        paragraphs(body, item.text, "muted");
        renderButtons(body, item.links, item.buttons);
      }
      li.appendChild(body);
      ol.appendChild(li);
    });
    return ol;
  }

  function readFile(file) {
    return new Promise(function (resolve, reject) {
      var reader = new FileReader();
      reader.onload = function () {
        var text = String(reader.result || "");
        resolve(text.slice(text.indexOf(",") + 1)); // base64 part of the data URL
      };
      reader.onerror = reject;
      reader.readAsDataURL(file);
    });
  }

  // A small form (text area and file fields) sent to one action; files go as base64.
  function renderForm(spec) {
    var form = el("form", "form");
    var inputs = {};
    (spec.fields || []).forEach(function (f) {
      var label = el("label", null, f.label);
      var input;
      if (f.type === "textarea") {
        input = el("textarea");
        input.rows = 4;
        input.setAttribute("autocomplete", "off");
        input.setAttribute("autocapitalize", "off");
        input.setAttribute("spellcheck", "false");
      } else {
        input = el("input");
        input.type = f.type || "text";
      }
      if (f.placeholder) input.placeholder = f.placeholder;
      input.name = f.name;
      inputs[f.name] = input;
      label.appendChild(input);
      form.appendChild(label);
    });
    var submit = el("button", "primary", spec.submit || "Send");
    submit.type = "submit";
    var btns = el("div", "btns");
    btns.appendChild(submit);
    form.appendChild(btns);
    form.addEventListener("submit", function (ev) {
      ev.preventDefault();
      var body = {};
      var pending = [];
      Object.keys(inputs).forEach(function (name) {
        var input = inputs[name];
        if (input.type === "file") {
          if (input.files && input.files[0]) {
            pending.push(readFile(input.files[0]).then(function (b64) {
              body[name] = b64;
            }));
          }
        } else {
          body[name] = input.value;
        }
      });
      Promise.all(pending).then(function () {
        runAction(submit, { action: spec.action, confirm: spec.confirm, body: body, form: form });
      }, function () {
        toast("That file could not be read.");
      });
    });
    return form;
  }

  // A card with something shown only on request: the 24 words or the phone QR code.
  function renderReveal(r) {
    if (r.pin) return renderPin(r);
    var card = el("section", "card reveal");
    if (r.id) card.id = r.id;
    card.appendChild(el("h2", null, r.title || ""));
    if (r.warn) card.appendChild(el("p", "warn", r.warn));
    if (r.words && r.words.length) {
      var ol = el("ol", "words");
      r.words.forEach(function (w) {
        ol.appendChild(el("li", null, w));
      });
      card.appendChild(ol);
    }
    if (r.qr) {
      var img = el("img", "qr");
      img.src = r.qr;
      img.alt = "QR code";
      card.appendChild(img);
    }
    if (r.steps && r.steps.length) {
      var steps = el("ol", "steps");
      r.steps.forEach(function (t) {
        steps.appendChild(el("li", null, t));
      });
      card.appendChild(steps);
    }
    paragraphs(card, r.text, "muted");
    var btns = el("div", "btns");
    if (r.copy) {
      var c = el("button", null, r.copy.label || "Copy");
      c.type = "button";
      c.addEventListener("click", function () {
        copyText(String(r.copy.value), c);
      });
      btns.appendChild(c);
    }
    (r.buttons || []).forEach(function (spec) {
      var b = el("button", spec.style || null, spec.label);
      b.type = "button";
      b.addEventListener("click", function () {
        runAction(b, spec);
      });
      btns.appendChild(b);
    });
    if (!(r.buttons && r.buttons.length)) {
      var close = el("button", null, "Hide");
      close.type = "button";
      close.addEventListener("click", function () {
        reveal = null;
        if (lastModel) render(lastModel);
      });
      btns.appendChild(close);
    }
    card.appendChild(btns);
    return card;
  }

  function render(model) {
    lastModel = model;
    document.title = model.title || "AVADO";
    document.getElementById("app-title").textContent = model.title || "AVADO";
    document.getElementById("app-version").textContent = model.version ? "version " + model.version : "";
    // Keep open "Details" boxes open across refreshes.
    var open = {};
    main.querySelectorAll("section[id] details[open]").forEach(function (d) {
      open[d.closest("section").id] = true;
    });
    var frag = document.createDocumentFragment();
    frag.appendChild(renderHero(model));
    var revealCard = reveal ? renderReveal(reveal) : null;
    if (revealCard) frag.appendChild(revealCard);
    if (model.facts && model.facts.length) frag.appendChild(renderFacts(model.facts));
    (model.sections || []).forEach(function (s) {
      var card = renderSection(s);
      if (s.id && open[s.id]) {
        var d = card.querySelector("details");
        if (d) d.open = true;
      }
      frag.appendChild(card);
    });
    main.replaceChildren(frag);
    if (revealCard && revealScroll) {
      revealScroll = false;
      revealCard.scrollIntoView({ block: "start", behavior: "smooth" });
    }
    // A link from another AVADO page can point at one card (#connect): show it once.
    if (!scrolled) {
      scrolled = true;
      var target = /^#[A-Za-z][\w-]*$/.test(location.hash) && document.getElementById(location.hash.slice(1));
      if (target) target.scrollIntoView({ block: "start" });
    }
    schedule(model.refresh || 10);
  }

  function schedule(seconds) {
    clearTimeout(timer);
    timer = setTimeout(load, Math.max(3, seconds) * 1000);
  }

  function load() {
    // Do not redraw while the owner answers an "are you sure?" (it would vanish).
    if (asking && main.contains(asking.box)) {
      schedule(10);
      return;
    }
    // Do not redraw a form the owner is filling in (it would lose what they typed).
    var filling = Array.prototype.some.call(main.querySelectorAll("form.form textarea, form.form input"), function (i) {
      return i.type === "file" ? !!(i.files && i.files.length) : !!i.value || i === document.activeElement;
    });
    if (filling && !busy) {
      schedule(10);
      return;
    }
    fetch("api/status", { cache: "no-store" })
      .then(function (resp) {
        if (!resp.ok) throw new Error("HTTP " + resp.status);
        return resp.json();
      })
      .then(render)
      .catch(function () {
        var card = el("section", "card hero state-warn");
        card.appendChild(el("span", "dot"));
        var body = el("div", "hero-body");
        body.appendChild(el("h1", null, "The app is starting"));
        body.appendChild(el("p", "muted", "This page updates by itself. If it stays like this for more than 10 minutes, restart the app in the AVADO Admin."));
        card.appendChild(body);
        main.replaceChildren(card);
        schedule(5);
      });
  }

  load();
})();
