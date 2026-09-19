// 身份桩的前端部分：姓名存 cookie，请求由浏览器自动携带；输入用页面内模态框。
(function () {
  var COOKIE = "fm_user";
  var modal = document.getElementById("identity-modal");
  var input = document.getElementById("identity-input");
  var who = document.getElementById("who");
  var initial = document.getElementById("who-initial");
  var menu = document.getElementById("who-menu");

  function readCookie(name) {
    var m = document.cookie.match(new RegExp("(?:^|; )" + name + "=([^;]*)"));
    return m ? decodeURIComponent(m[1]) : null;
  }

  function writeCookie(name, value) {
    var d = new Date();
    d.setTime(d.getTime() + 365 * 86400000);
    document.cookie = name + "=" + encodeURIComponent(value) +
      ";expires=" + d.toUTCString() + ";path=/;samesite=lax";
  }

  function paint(name) {
    who.textContent = name || "未设置";
    initial.textContent = name ? name.trim().charAt(0) : "·";
  }

  function openModal(placeholder, required) {
    input.value = placeholder || "";
    modal.dataset.required = required ? "1" : "";
    // 没有身份时不给取消：否则上传会因为 401 失败，用户却不知道原因
    document.getElementById("identity-cancel").hidden = !!required;
    modal.hidden = false;
    input.focus();
    input.select();
  }

  function closeModal() {
    // 首次进入且没有身份时不允许取消，否则页面上的写操作不知道该记在谁头上
    if (!modal.dataset.required) modal.hidden = true;
  }

  function save() {
    var name = input.value.trim();
    if (!name) {
      input.focus();
      return;
    }
    var changed = name !== readCookie(COOKIE);
    writeCookie(COOKIE, name);
    if (changed) window.location.reload();  // 页面按新身份重渲染（下拉选中项等）
    else { paint(name); modal.hidden = true; }
  }

  document.getElementById("change-who").addEventListener("click", function () {
    if (menu) menu.open = false;
    openModal(readCookie(COOKIE), false);
  });
  document.getElementById("identity-save").addEventListener("click", save);
  document.getElementById("identity-cancel").addEventListener("click", function () {
    modal.hidden = true;
  });
  input.addEventListener("keydown", function (e) {
    if (e.key === "Enter") save();
    if (e.key === "Escape") closeModal();
  });
  modal.addEventListener("click", function (e) {
    if (e.target === modal) closeModal();
  });

  // 点头像之外或按 Esc 收起下拉
  document.addEventListener("click", function (e) {
    if (menu && menu.open && !menu.contains(e.target)) menu.open = false;
  });
  document.addEventListener("keydown", function (e) {
    if (e.key === "Escape" && menu) menu.open = false;
  });

  var current = readCookie(COOKIE);
  paint(current);
  if (!current) openModal("", true);
})();
