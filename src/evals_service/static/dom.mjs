/**
 * **********************************************************
 * @Author: Andreas Paepcke
 * @Date:   2026-10-11 16:46:16
 * @File:   /Users/paepcke/VSCodeWorkspaces/therapist-exp-evals/src/evals_service/static/dom.mjs
 * @Last Modified by:   Andreas Paepcke
 * @Last Modified time: 2026-10-11 16:46:16
 * **********************************************************
 */

'use strict';

/*
 * Small helpers for building the page. Text always goes in with textContent,
 * never as markup. The one exception is the HTML of a session, which the
 * server's bundle check has already vetted; Dom.sessionHtml() filters it
 * once more with an allow-list before it touches the page.
 */

export class Dom {
  /**
   * @param {string} tag
   * @param {Object} attrs  class, text, and on<event> functions have special meaning;
   *                        everything else is set as an attribute (false/null are skipped)
   * @param {...(Node|string|null)} kids
   * @returns {HTMLElement}
   */
  static el(tag, attrs = {}, ...kids) {
    const node = document.createElement(tag);
    for (const [key, value] of Object.entries(attrs || {})) {
      if (value === false || value === null || value === undefined) continue;
      if (key === 'class') node.className = value;
      else if (key === 'text') node.textContent = value;
      else if (key.startsWith('on') && typeof value === 'function') node.addEventListener(key.slice(2), value);
      else node.setAttribute(key, value === true ? '' : String(value));
    }
    for (const kid of kids.flat()) {
      if (kid === null || kid === undefined || kid === false) continue;
      node.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
    }
    return node;
  }

  static clear(node) {
    while (node.firstChild) node.removeChild(node.firstChild);
    return node;
  }

  /**
   * "On average, when the trainer acts as **client**, ..." -> text with <strong>.
   * @param {string} text
   * @returns {DocumentFragment}
   */
  static emphasized(text) {
    const frag = document.createDocumentFragment();
    String(text).split('**').forEach((part, i) => {
      if (part === '') return;
      frag.append(i % 2 === 1 ? Dom.el('strong', { text: part }) : document.createTextNode(part));
    });
    return frag;
  }

  /**
   * Put a session's HTML into the page, keeping only known harmless elements.
   * @param {HTMLElement} container
   * @param {string} html
   */
  static sessionHtml(container, html) {
    const allowedTags = new Set(['SECTION', 'HEADER', 'H2', 'DIV', 'SPAN', 'P', 'BR', 'DETAILS', 'SUMMARY']);
    const allowedAttrs = new Set(['class', 'title']);
    const parsed = new DOMParser().parseFromString(`<body>${html}</body>`, 'text/html').body;
    const copy = (source, target) => {
      for (const child of source.childNodes) {
        if (child.nodeType === Node.TEXT_NODE) {
          target.append(document.createTextNode(child.nodeValue));
        } else if (child.nodeType === Node.ELEMENT_NODE && allowedTags.has(child.tagName)) {
          const clone = document.createElement(child.tagName.toLowerCase());
          for (const attr of child.attributes) {
            if (allowedAttrs.has(attr.name)) clone.setAttribute(attr.name, attr.value);
          }
          copy(child, clone);
          target.append(clone);
        }
      }
    };
    Dom.clear(container);
    copy(parsed, container);
  }
}
