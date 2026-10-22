/* Applique le theme memorise avant le rendu (evite un flash clair).

   Chargé par index.html et login.html ; doit rester un fichier separe pour
   que la politique de securite du contenu puisse interdire les scripts
   inline (`script-src` sans 'unsafe-inline').
*/
(function () {
  var theme = "dark";
  try { theme = localStorage.getItem("aicv-theme") || "dark"; } catch (e) {}
  document.documentElement.setAttribute("data-theme", theme);
  // Chrome : palette bordeaux imposee, pas de bascule clair/nuit
  var isChrome = /Chrome/.test(navigator.userAgent) && /Google Inc/.test(navigator.vendor);
  if (isChrome) document.documentElement.setAttribute("data-force-chrome", "");
})();
