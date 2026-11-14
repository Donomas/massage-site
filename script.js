"use strict";

const heroImage = document.querySelector("#hero-image");
const heroVisual = document.querySelector("[data-hero-visual]");

if (heroImage && heroVisual) {
  if (heroImage.complete) {
    if (!heroImage.naturalWidth) heroVisual.classList.add("is-unavailable");
  } else {
    // Let fast requests finish before showing a loading indicator.
    const indicatorTimer = window.setTimeout(() => {
      if (!heroImage.complete) heroVisual.classList.add("is-loading");
    }, 180);

    const settleImage = (className) => {
      window.clearTimeout(indicatorTimer);
      heroVisual.classList.remove("is-loading");
      heroVisual.classList.add(className);
    };

    heroImage.addEventListener("load", () => settleImage("has-loaded"), { once: true });
    heroImage.addEventListener("error", () => settleImage("is-unavailable"), { once: true });
  }
}
