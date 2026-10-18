// Added by Trackinizer to the HTML Artifact pages it serves. When the canvas around
// this page asks for a picture, this page draws itself, because the canvas cannot
// read the pixels of a sandboxed frame. It does nothing until asked.
(function () {
  "use strict";
  var LIBRARY = "https://cdn.jsdelivr.net/npm/html-to-image@1.11.13/dist/html-to-image.js";
  var INTEGRITY = "sha384-Tha/42qsYmpYmQ07pX+nJzkKumO0BzKJxK/uzVc7xyBQxVCUgQBhQIG8L7vXK+9C";
  var loading = null;

  function load() {
    if (window.htmlToImage) return Promise.resolve(window.htmlToImage);
    if (loading === null) {
      loading = new Promise(function (resolve, reject) {
        var script = document.createElement("script");
        script.src = LIBRARY;
        script.integrity = INTEGRITY;
        script.crossOrigin = "anonymous";
        script.onload = function () { resolve(window.htmlToImage); };
        script.onerror = function () { loading = null; reject(new Error("the capture library did not load")); };
        document.head.appendChild(script);
      });
    }
    return loading;
  }

  function reply(id, fields) {
    // The frame's origin is opaque and the parent's is unknown to it; the page that
    // embeds this one is limited to Trackinizer by frame-ancestors.
    window.parent.postMessage(Object.assign({ type: "trax-capture-result", id: id }, fields), "*");
  }

  function capture(id) {
    load()
      .then(function (library) {
        // The viewport as scrolled: the whole document, moved up and left, cut to the window.
        return library.toPng(document.documentElement, {
          width: window.innerWidth,
          height: window.innerHeight,
          pixelRatio: 1,
          skipFonts: true,
          style: { transform: "translate(" + -window.scrollX + "px," + -window.scrollY + "px)" },
        });
      })
      .then(
        function (png) { reply(id, { png: png, width: window.innerWidth, height: window.innerHeight }); },
        function (error) { reply(id, { error: String(error && error.message ? error.message : error) }); },
      );
  }

  window.addEventListener("message", function (event) {
    var data = event.data;
    if (event.source !== window.parent || !data || data.type !== "trax-capture" || typeof data.id !== "string") return;
    capture(data.id);
  });
})();
