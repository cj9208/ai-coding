/* 身份桩：cookie 记名，无登录。未署名时顶栏出现内联输入框，一次输入长期记住。 */
(function () {
  var NAME = "ocr_review_user";

  function read() {
    var m = document.cookie.match(new RegExp("(?:^|; )" + NAME + "=([^;]*)"));
    return m ? decodeURIComponent(m[1]) : "";
  }

  function setName(name) {
    fetch("/api/who?name=" + encodeURIComponent(name), { method: "POST" })
      .then(function () { render(); })
      .catch(function () {});
  }

  function render() {
    var box = document.getElementById("identity");
    var name = read();
    if (name) {
      box.innerHTML =
        '<span class="who">' + escapeHtml(name) + '</span>' +
        '<button type="button" id="who-change" class="ghost">更换</button>';
      document.getElementById("who-change").addEventListener("click", edit);
    } else {
      edit();
    }
  }

  function edit() {
    var box = document.getElementById("identity");
    box.innerHTML =
      '<input type="text" id="who-input" placeholder="你的姓名（记录修改人）" ' +
      'value="' + escapeHtml(read()) + '">' +
      '<button type="button" id="who-save">署名</button>';
    var input = document.getElementById("who-input");
    input.focus();
    document.getElementById("who-save").addEventListener("click", function () {
      var v = input.value.trim();
      if (v) setName(v);
    });
    input.addEventListener("keydown", function (evt) {
      if (evt.key === "Enter") document.getElementById("who-save").click();
    });
  }

  function escapeHtml(s) {
    return String(s).replace(/[&<>"]/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c];
    });
  }

  document.addEventListener("DOMContentLoaded", render);
})();
