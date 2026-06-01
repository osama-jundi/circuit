/* ============================================================
   dialogs.js — styled, promise-based replacements for the
   browser's prompt() / confirm(). Shared by every page.

   uiPrompt(opts)  -> Promise<string|null>   (null = cancelled)
   uiConfirm(opts) -> Promise<bool>
   Each opts: { title, message, value, placeholder, okLabel, danger }
   ============================================================ */
(function () {
  function build() {
    if (document.getElementById("ui-dialog-overlay")) return;
    const o = document.createElement("div");
    o.id = "ui-dialog-overlay";
    o.innerHTML = `
      <div id="ui-dialog" role="dialog" aria-modal="true">
        <h3 id="ui-dialog-title"></h3>
        <div id="ui-dialog-msg"></div>
        <input id="ui-dialog-input" type="text" style="display:none">
        <div id="ui-dialog-actions">
          <button id="ui-dialog-cancel" type="button">Cancel</button>
          <button id="ui-dialog-ok" type="button">OK</button>
        </div>
      </div>`;
    document.body.appendChild(o);

    const css = document.createElement("style");
    css.textContent = `
      #ui-dialog-overlay { position: fixed; inset: 0; background: rgba(15,25,40,.5);
        display: none; align-items: center; justify-content: center; z-index: 4000; }
      #ui-dialog-overlay.open { display: flex; }
      #ui-dialog { background: #fff; width: 380px; max-width: 92vw; border-radius: 10px;
        box-shadow: 0 20px 60px rgba(0,0,0,.4); padding: 20px 22px;
        font-family: -apple-system, "Segoe UI", Roboto, sans-serif; }
      #ui-dialog h3 { margin: 0 0 10px; font-size: 16px; color: #1e3a5f; }
      #ui-dialog-msg { font-size: 13px; color: #475467; line-height: 1.5; white-space: pre-wrap; }
      #ui-dialog-input { width: 100%; box-sizing: border-box; margin-top: 14px; padding: 9px 11px;
        font-size: 14px; border: 1px solid #d0d5dd; border-radius: 6px; outline: none; }
      #ui-dialog-input:focus { border-color: #2a4d7a; }
      #ui-dialog-actions { display: flex; justify-content: flex-end; gap: 8px; margin-top: 18px; }
      #ui-dialog-actions button { padding: 8px 16px; font-size: 13px; font-weight: 600;
        border-radius: 6px; cursor: pointer; border: 1px solid transparent; }
      #ui-dialog-cancel { background: #eceef2; color: #344054; }
      #ui-dialog-cancel:hover { background: #dde1e7; }
      #ui-dialog-ok { background: #1e3a5f; color: #fff; }
      #ui-dialog-ok:hover { background: #2a4d7a; }
      #ui-dialog-ok.danger { background: #d92d20; }
      #ui-dialog-ok.danger:hover { background: #b42318; }`;
    document.head.appendChild(css);
  }

  function open(opts, withInput) {
    build();
    return new Promise((resolve) => {
      const overlay = document.getElementById("ui-dialog-overlay");
      const titleEl = document.getElementById("ui-dialog-title");
      const msgEl = document.getElementById("ui-dialog-msg");
      const input = document.getElementById("ui-dialog-input");
      const okBtn = document.getElementById("ui-dialog-ok");
      const cancelBtn = document.getElementById("ui-dialog-cancel");

      titleEl.textContent = opts.title || "";
      msgEl.textContent = opts.message || "";
      msgEl.style.display = opts.message ? "block" : "none";
      okBtn.textContent = opts.okLabel || "OK";
      okBtn.classList.toggle("danger", !!opts.danger);

      if (withInput) {
        input.style.display = "block";
        input.value = opts.value || "";
        input.placeholder = opts.placeholder || "";
      } else {
        input.style.display = "none";
      }

      overlay.classList.add("open");
      if (withInput) { input.focus(); input.select(); } else { okBtn.focus(); }

      function cleanup(result) {
        overlay.classList.remove("open");
        okBtn.onclick = cancelBtn.onclick = overlay.onclick = input.onkeydown = document.onkeydown = null;
        resolve(result);
      }
      const accept = () => cleanup(withInput ? input.value : true);
      const reject = () => cleanup(withInput ? null : false);

      okBtn.onclick = accept;
      cancelBtn.onclick = reject;
      overlay.onclick = (e) => { if (e.target === overlay) reject(); };
      input.onkeydown = (e) => { if (e.key === "Enter") accept(); };
      document.onkeydown = (e) => { if (e.key === "Escape") reject(); };
    });
  }

  window.uiPrompt = (opts) => open(opts || {}, true);
  window.uiConfirm = (opts) => open(opts || {}, false);
})();
