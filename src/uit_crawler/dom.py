"""Browser-side readers for visible Facebook markup, without generated classes."""

# Kept as one evaluated function so each read sees one coherent DOM snapshot.
SCAN = r"""({mode, target, sourceId, includeAuthors}) => {
  const state = window.__uitReadOnlyCrawler || (window.__uitReadOnlyCrawler = {
    ids: new WeakMap(), next: 0, controls: new Map()
  });
  state.controls.clear();
  const id = node => {
    if (!state.ids.has(node)) state.ids.set(node, String(++state.next));
    return state.ids.get(node);
  };
  const visible = node => !!node && node.getClientRects().length > 0 &&
    getComputedStyle(node).visibility !== 'hidden' && !node.closest('[aria-hidden="true"]');
  const insideLink = node => !!node.closest('a[href], [role="link"]');
  const messageSelector = '[data-ad-preview="message"], [data-ad-comet-preview="message"], [data-ad-rendering-role="story_message"]';
  const commentSelector = '[data-ad-comet-preview="comment"], [data-ad-preview="comment"]';
  const containerSelector = '[role="article"], [aria-posinset]';
  const articles = [...document.querySelectorAll(containerSelector)].filter(visible);
  const own = (node, article) => node.closest(containerSelector) === article;
  const links = article => [...article.querySelectorAll('a[href]')].filter(a => visible(a) && own(a, article));
  const isCommentLink = a => {
    try { return new URL(a.href).searchParams.has('comment_id'); } catch { return false; }
  };
  const ownMessage = article => [...article.querySelectorAll(messageSelector)].find(n => own(n, article));
  const isComment = article => /^(comment by|reply by|bình luận (?:của|dưới tên)|phản hồi(?: bình luận)?|câu trả lời của)\b/iu.test(article.getAttribute('aria-label') || '') ||
    links(article).some(isCommentLink);
  const emojiAlt = image => {
    const alt = image.getAttribute('alt') || '';
    return /\/emoji\.php(?:\/|\?)/.test(image.getAttribute('src') || '') ||
      /^[\p{Extended_Pictographic}\p{Regional_Indicator}\p{Emoji_Modifier}\u200d\ufe0f\u20e3]+$/u.test(alt) ? alt : '';
  };
  const text = node => {
    if (!node) return '';
    const visit = current => {
      if (current.nodeType === Node.TEXT_NODE) return current.textContent;
      if (current.nodeType !== Node.ELEMENT_NODE) return '';
      if (current !== node && (current.matches('[role="article"], [aria-posinset], [role="button"], button, [aria-hidden="true"], script, style') || !visible(current))) return '';
      if (current !== node && current.matches(messageSelector) && insideLink(current)) return '';
      if (current.tagName === 'IMG') return emojiAlt(current);
      if (current.tagName === 'BR') return '\n';
      const content = [...current.childNodes].map(visit).join('');
      return /^(DIV|P|LI|BLOCKQUOTE)$/.test(current.tagName) ? '\n' + content + '\n' : content;
    };
    return visit(node).replace(/[\t \u00a0]+/g, ' ').replace(/ *\n */g, '\n').replace(/\n{3,}/g, '\n\n').trim();
  };
  const bodies = (article, comment) => {
    let nodes = [...article.querySelectorAll(comment ? commentSelector + ', ' + messageSelector : messageSelector)].filter(n => own(n, article) && visible(n) && !insideLink(n));
    if (!comment && nodes.length) nodes = [nodes[0]];
    if (!nodes.length) nodes = [...article.querySelectorAll('[dir="auto"][style*="text-align" i], [data-ad-rendering-role="story_message"]')].filter(n => own(n, article) && visible(n) && !n.closest('a, [role="link"], [role="button"], button'));
    if (!nodes.length && comment) nodes = [...article.querySelectorAll('[dir="auto"]')].filter(n => own(n, article) && visible(n) && !n.closest('a, [role="link"], [role="button"], button'));
    return nodes.filter(n => !nodes.some(other => other !== n && other.contains(n)));
  };
  const timestamp = anchor => {
    if (!anchor) return {published_text: null, published_at: null};
    const timed = anchor.matches('time, abbr') ? anchor : anchor.querySelector('time[datetime], abbr[data-utime]');
    let machine = timed?.getAttribute('datetime') || null;
    const epoch = timed?.getAttribute('data-utime');
    if (!machine && epoch && /^\d{9,13}$/.test(epoch)) machine = new Date(Number(epoch) * (epoch.length <= 10 ? 1000 : 1)).toISOString();
    // Titles are accepted as machine dates only when explicitly ISO formatted.
    const title = anchor.getAttribute('title');
    if (!machine && title && /^\d{4}-\d\d-\d\dT\d\d:\d\d/.test(title)) machine = title;
    return {published_text: anchor.getAttribute('aria-label') || text(anchor) || null, published_at: machine};
  };
  const postLinks = article => {
    const body = ownMessage(article);
    return links(article).filter(a => !isCommentLink(a) && !a.closest('blockquote') &&
      (!body || (!body.contains(a) && !a.contains(body) && !!(a.compareDocumentPosition(body) & Node.DOCUMENT_POSITION_FOLLOWING))));
  };
  const isMediaLink = anchor => {
    try {
      const url = new URL(anchor.href);
      return /(^|\.)facebook\.com$/i.test(url.hostname) && /\/(?:photo(?:\.php)?\/?$|photos\/|videos\/|reel\/)/.test(url.pathname);
    } catch { return false; }
  };
  const hasMedia = article => links(article).some(isMediaLink);
  const contentVisuals = article => [...article.querySelectorAll('img, video')].filter(node => {
    if (!own(node, article) || !visible(node) || node.matches('[role="presentation"], [role="none"]') ||
        node.closest('[contenteditable="true"]')) return false;
    const linked = node.closest('a[href], [role="link"]');
    if (linked && !isMediaLink(linked)) return false;
    return !/^(?:profile picture|ảnh đại diện)\b/iu.test(node.getAttribute('alt') || '');
  });
  if (mode === 'discover') {
    return {articles: articles.filter(a => !isComment(a) && !a.parentElement?.closest(containerSelector)).map(a => ({
      links: postLinks(a).map(link => {
        const token = id(link), raw = link.getAttribute('href') || '';
        state.controls.set(token, link);
        // Logged-in timestamps can be placeholders until a normal mouse hover.
        const lazy = /^[?#]/.test(raw) && !isCommentLink(link);
        return {url: link.href, label: link.getAttribute('aria-label') || text(link), token, lazy};
      })
    })), height: document.scrollingElement?.scrollHeight || 0};
  }
  const matchesTarget = anchor => {
    try {
      const url = new URL(anchor.href), wanted = new URL(target);
      if (url.searchParams.has('comment_id')) return false;
      const path = url.pathname.replace(/\/$/, '');
      return path === wanted.pathname.replace(/\/$/, '') &&
        (!wanted.searchParams.has('story_fbid') || url.searchParams.get('story_fbid') === wanted.searchParams.get('story_fbid')) ||
        (sourceId && (url.searchParams.get('story_fbid') === sourceId || path.split('/').at(-1) === sourceId));
    } catch { return false; }
  };
  const posts = articles.filter(a => !isComment(a));
  let root = posts.find(a => postLinks(a).some(matchesTarget));
  // Permalink dialogs sometimes omit their own timestamp. Only accept an
  // unambiguous message-bearing article in the visible dialog/main region.
  if (!root) {
    const dialogs = [...document.querySelectorAll('[role="dialog"]')].filter(visible);
    const region = dialogs.at(-1) || document.querySelector('[role="main"]');
    const candidates = posts.filter(a => (!region || region.contains(a)) && ownMessage(a) && !a.parentElement?.closest(containerSelector));
    if (candidates.length === 1) root = candidates[0];
  }
  if (!root) {
    const region = document.querySelector('[role="main"]') || document.body;
    const walker = document.createTreeWalker(region, NodeFilter.SHOW_TEXT);
    const notices = [];
    let node;
    while ((node = walker.nextNode())) {
      const parent = node.parentElement;
      if (parent && visible(parent) && !parent.closest('[role="article"], [aria-posinset], [data-ad-preview], [data-ad-comet-preview], script, style')) notices.push(node.textContent);
    }
    return {missing: true, visibleText: notices.join(' ').slice(0, 12000)};
  }
  if (mode === 'ready') return {ready: bodies(root, false).length > 0 || !!ownMessage(root) || hasMedia(root)};
  // Facebook can place comments beside the article within a permalink dialog.
  // Widen only when that dialog contains exactly this one post.
  const dialog = root.closest('[role="dialog"]');
  const scope = dialog && posts.filter(a => dialog.contains(a)).length === 1 ? dialog : root;
  state.scope = scope;
  const postBodyNodes = bodies(root, false);
  const commentBodies = new Map();
  const commentArticles = articles.filter(a => scope.contains(a) && a !== root && isComment(a));
  const comments = commentArticles.map(article => {
    const candidates = links(article).filter(isCommentLink);
    candidates.sort((a, b) => Number(!/\/(posts|videos)\/|\/(permalink|story)\.php/.test(a.href)) - Number(!/\/(posts|videos)\/|\/(permalink|story)\.php/.test(b.href)));
    const bodyNodes = bodies(article, true);
    commentBodies.set(article, bodyNodes);
    const hasMoreText = [...article.querySelectorAll('[role="button"], button')].some(node =>
      own(node, article) && visible(node) && !insideLink(node) && bodyNodes.some(body => body.contains(node)) && /^(See more|Xem thêm)$/iu.test((node.innerText || node.getAttribute('aria-label') || '').trim()));
    let bodyText = bodyNodes.map(text).filter(Boolean).join('\n\n');
    let visualMedia = false;
    if (!bodyText) {
      for (const node of contentVisuals(article)) {
        const emoji = node.tagName === 'IMG' ? emojiAlt(node) : '';
        if (emoji) bodyText += emoji;
        else visualMedia = true;
      }
    }
    const kind = bodyText ? 'text' : visualMedia ? 'media' : bodyNodes.length ? 'text' : 'unknown';
    const enclosing = article.parentElement?.closest(containerSelector);
    const isPostLink = a => /\/(posts|videos)\/|\/(permalink|story)\.php/.test(a.href);
    const author = includeAuthors ? links(article).find(a => !isPostLink(a) && !isMediaLink(a) && text(a).length > 0) : null;
    return {node_id: id(article), links: candidates.map(a => a.href), text: bodyText, content_kind: kind,
      has_body: kind !== 'unknown', text_expanded: kind !== 'unknown' && !hasMoreText,
      enclosing_node_id: enclosing && isComment(enclosing) ? id(enclosing) : null,
      author_name: author ? text(author) || null : null, ...timestamp(candidates.find(a => /\/(posts|videos)\/|\/(permalink|story)\.php/.test(a.href)))};
  });
  const controls = [...scope.querySelectorAll('[role="button"], button, [role="combobox"]')].filter(node => {
    const article = node.closest(containerSelector);
    return visible(node) && !insideLink(node) && (!article || article === root || isComment(article));
  }).map(node => {
    const token = id(node);
    state.controls.set(token, node);
    const comment = node.closest(containerSelector);
    const inComment = !!comment && comment !== root && isComment(comment);
    const textBodies = inComment ? (commentBodies.get(comment) || []) : postBodyNodes;
    return {token, label: (node.innerText || node.getAttribute('aria-label') || '').replace(/\s+/g, ' ').trim(),
      disabled: node.getAttribute('aria-disabled') === 'true' || !!node.disabled,
      in_comment: inComment, text_expander: textBodies.some(body => body.contains(node))};
  });
  const orphanLinks = [...scope.querySelectorAll('a[href]')].filter(a => visible(a) && isCommentLink(a) && !commentArticles.some(c => c.contains(a)));
  const countPattern = /^(?:[\d.,\s]+(?:K|M|k|m|N|Tr)?\s+(?:comments?|bình luận)|(?:comments?|bình luận)\s*[:：]?\s*[\d.,]+)$/iu;
  const count = [...scope.querySelectorAll('span, [role="button"]')].filter(visible).map(n => (n.innerText || '').trim()).find(t => countPattern.test(t)) || null;
  return {missing: false, text: postBodyNodes.map(text).filter(Boolean).join('\n\n'), has_body: postBodyNodes.length > 0 || !!ownMessage(root),
    comments, controls, orphan_comments: orphanLinks.length, ui_comment_count: count, has_media: hasMedia(root),
    ...timestamp(postLinks(root).find(matchesTarget)), scope_token: id(scope)};
}"""

CONTROL = "token => window.__uitReadOnlyCrawler?.controls.get(token) || null"

SAFE_CONTROL = r"""token => {
  const node = window.__uitReadOnlyCrawler?.controls.get(token);
  return node?.isConnected && !node.closest('a[href], [role=\"link\"]') ? node : null;
}"""

SCROLL = r"""discovery => {
  const scope = discovery ? null : window.__uitReadOnlyCrawler?.scope;
  const candidates = scope ? [scope, ...scope.querySelectorAll('*')] : [];
  for (let ancestor = scope?.parentElement; ancestor; ancestor = ancestor.parentElement) candidates.push(ancestor);
  const scroller = candidates.find(n => /auto|scroll/.test(getComputedStyle(n).overflowY) && n.scrollHeight > n.clientHeight + 10) || document.scrollingElement;
  if (!scroller) return false;
  const before = scroller.scrollTop;
  scroller.scrollBy(0, Math.max(300, scroller.clientHeight * 0.8));
  return scroller.scrollTop !== before;
}"""
