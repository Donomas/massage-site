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

window.addEventListener("pageshow", showCurrentTab);
mobileTabs.addEventListener("change", showCurrentTab);
