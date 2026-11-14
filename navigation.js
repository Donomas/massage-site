"use strict";

const mobileTabs = window.matchMedia("(max-width: 760px)");
const tabList = document.querySelector(".nav-list");
const currentTab = tabList?.querySelector('[aria-current="page"]');

function showCurrentTab() {
  if (!mobileTabs.matches || !currentTab) return;

  const strip = tabList.getBoundingClientRect();
  const tab = currentTab.getBoundingClientRect();
  const edge = 8;
  if (tab.left < strip.left + edge || tab.right > strip.right - edge) {
    // Move the tab strip without changing the document's scroll position.
    tabList.scrollLeft += tab.left + tab.width / 2 - (strip.left + strip.width / 2);
  }
}

let previousWidth = window.innerWidth;
window.addEventListener("pageshow", showCurrentTab);
window.addEventListener("resize", () => {
  if (window.innerWidth === previousWidth) return;
  previousWidth = window.innerWidth;
  showCurrentTab();
});
