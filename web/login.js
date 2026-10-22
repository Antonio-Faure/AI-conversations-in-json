/* Connexion a l'interface (page /login). */
(function () {
  "use strict";
  var form = document.getElementById("loginForm");
  var input = document.getElementById("password");
  var reveal = document.getElementById("reveal");
  var button = document.getElementById("submit");
  var errorBox = document.getElementById("error");

  /* destination apres connexion : chemin interne uniquement (pas d'URL
     externe, pas de caractere de controle ni d'antislash) */
  function nextTarget() {
    var raw = new URLSearchParams(window.location.search).get("next") || "/";
    if (!/^\/(?!\/)[^\u0000-\u001f\\]*$/.test(raw)) return "/";
    return raw;
  }

  reveal.addEventListener("click", function () {
    var shown = input.type === "text";
    input.type = shown ? "password" : "text";
    reveal.setAttribute("aria-pressed", String(!shown));
    reveal.setAttribute("aria-label", shown ? "Afficher le mot de passe" : "Masquer le mot de passe");
    input.focus();
  });

  function failed(message) {
    errorBox.textContent = message;
    button.disabled = false;
    button.textContent = "Se connecter";
    input.select();
  }

  form.addEventListener("submit", function (event) {
    event.preventDefault();
    var password = input.value;
    if (!password) {
      errorBox.textContent = "saisissez le mot de passe";
      input.focus();
      return;
    }
    errorBox.textContent = "";
    button.disabled = true;
    button.textContent = "Connexion…";
    fetch("/api/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ password: password })
    }).then(function (response) {
      return response.json().catch(function () { return {}; }).then(function (data) {
        if (response.ok) {
          window.location.replace(nextTarget());
          return;
        }
        failed(data.error || "connexion impossible");
      });
    }).catch(function () {
      failed("serveur injoignable");
    });
  });
})();
