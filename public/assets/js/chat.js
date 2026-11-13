(function () {
  "use strict";

  var root = document.getElementById("chat");
  if (!root) return;

  var log = document.getElementById("chat-log");
  var form = document.getElementById("chat-form");
  var input = document.getElementById("chat-input");
  var sendButton = document.getElementById("chat-send");
  var resetButton = document.getElementById("chat-reset");
  var statusLine = document.getElementById("chat-status");
  var quick = document.getElementById("chat-quick");

  var apiUrl = root.getAttribute("data-api-url");
  var resetUrl = root.getAttribute("data-reset-url");
  var loginUrl = root.getAttribute("data-login-url") || "/login";
  var aiEnabled = root.getAttribute("data-ai-enabled") === "1";
  var maxMessage = parseInt(root.getAttribute("data-max-message"), 10) || 300;
  var csrfMeta = document.querySelector('meta[name="csrf-token"]');
  var csrfToken = csrfMeta ? csrfMeta.getAttribute("content") : "";

  // 같은 탭에서 다른 계정으로 로그인해도 앞사람 대화가 보이지 않게 계정별로 나눠 둔다.
  var storageKey = "bungae.chat.v1." + (root.getAttribute("data-user-id") || "0");
  var MAX_STORED = 40;

  var messages = [];
  var busy = false;
  var typingNode = null;

  // ---- 저장 (sessionStorage를 못 쓰는 환경에서도 채팅은 동작해야 한다) ----
  function saveMessages() {
    try {
      window.sessionStorage.setItem(storageKey, JSON.stringify(messages.slice(-MAX_STORED)));
    } catch (error) {
      /* 저장 실패는 무시한다 */
    }
  }

  function loadMessages() {
    try {
      var raw = window.sessionStorage.getItem(storageKey);
      var parsed = raw ? JSON.parse(raw) : [];
      if (!Array.isArray(parsed)) return [];
      return parsed.slice(-MAX_STORED).map(normalizeStored).filter(Boolean);
    } catch (error) {
      return [];
    }
  }

  function clearMessages() {
    try {
      window.sessionStorage.removeItem(storageKey);
    } catch (error) {
      /* 무시 */
    }
  }

  // ---- 값 정리: 서버 응답이든 저장해 둔 값이든 형식을 확인하고 쓴다 ----
  function text(value, max) {
    return typeof value === "string" ? value.slice(0, max || 400) : "";
  }

  function textList(value, maxItems) {
    if (!Array.isArray(value)) return [];
    return value
      .filter(function (item) { return typeof item === "string" && item; })
      .slice(0, maxItems || 8)
      .map(function (item) { return item.slice(0, 80); });
  }

  // 링크는 사이트 안쪽 경로만 허용한다.
  function sitePath(value) {
    if (typeof value !== "string" || value.charAt(0) !== "/") return null;
    var second = value.charAt(1);
    if (second === "/" || second === "\\") return null;
    return value.slice(0, 500);
  }

  function normalizeMeeting(raw) {
    if (!raw || typeof raw !== "object") return null;
    var url = sitePath(raw.url);
    if (!url) return null;
    return {
      title: text(raw.title, 80),
      category: text(raw.category, 30),
      emoji: text(raw.emoji, 8),
      when: text(raw.when, 40),
      relative: text(raw.relative, 30),
      place: text(raw.place, 80),
      seats: text(raw.seats, 20),
      reasons: textList(raw.reasons, 4),
      url: url
    };
  }

  function normalizeBot(raw) {
    var meetings = Array.isArray(raw.meetings) ? raw.meetings.map(normalizeMeeting).filter(Boolean).slice(0, 8) : [];
    return {
      role: "bot",
      reply: text(raw.reply, 600),
      mode: raw.mode === "ai" || raw.mode === "rule" ? raw.mode : "",
      understood: textList(raw.understood, 10),
      meetings: meetings,
      create_url: sitePath(raw.create_url),
      error: raw.error === true
    };
  }

  function normalizeStored(raw) {
    if (!raw || typeof raw !== "object") return null;
    if (raw.role === "user") {
      var body = text(raw.text, maxMessage);
      return body ? { role: "user", text: body } : null;
    }
    if (raw.role === "bot") return normalizeBot(raw);
    return null;
  }

  // ---- 화면 그리기: 받은 문자열은 전부 textContent로만 넣는다 ----
  function el(tag, className, content) {
    var node = document.createElement(tag);
    if (className) node.className = className;
    if (content) node.textContent = content;
    return node;
  }

  function renderCard(meeting, stale) {
    var item = el("li");
    var link = el("a", "mcard");
    link.setAttribute("href", meeting.url);

    var top = el("div", "mcard__top");
    top.appendChild(el("span", "pill", (meeting.emoji ? meeting.emoji + " " : "") + meeting.category));
    link.appendChild(top);

    link.appendChild(el("h3", "mcard__title", meeting.title));

    var when = el("p", "mcard__meta");
    when.appendChild(el("span", "", "🕒 " + meeting.when));
    // 다시 그린 대화의 '30분 뒤'는 이미 틀린 값이라 보여 주지 않는다.
    if (meeting.relative && !stale) when.appendChild(el("span", "mcard__rel", meeting.relative));
    link.appendChild(when);

    var where = el("p", "mcard__meta");
    where.appendChild(el("span", "", "📍 " + meeting.place));
    where.appendChild(el("span", "mcard__seats", "👥 " + meeting.seats));
    link.appendChild(where);

    if (meeting.reasons.length) {
      var reasons = el("ul", "reasons");
      meeting.reasons.forEach(function (reason) {
        reasons.appendChild(el("li", "reason", reason));
      });
      link.appendChild(reasons);
    }

    item.appendChild(link);
    return item;
  }

  function renderMessage(message, stale) {
    if (message.role === "user") {
      var mine = el("div", "msg msg--user");
      mine.appendChild(el("p", "bubble", message.text));
      return mine;
    }

    var node = el("div", "msg msg--bot");
    if (message.error) node.classList.add("msg--error");
    node.appendChild(el("p", "bubble", message.reply));

    if (message.mode) {
      var badge = el("span", "badge badge--mode", message.mode === "ai" ? "AI 해석" : "기본 해석");
      if (message.mode === "ai") badge.classList.add("badge--mode-ai");
      node.appendChild(badge);
    }

    if (message.understood.length) {
      var understood = el("div", "msg__understood");
      understood.appendChild(el("span", "msg__label", "이렇게 이해했어요"));
      var chips = el("ul", "chips");
      message.understood.forEach(function (label) {
        chips.appendChild(el("li", "chip chip--plain", label));
      });
      understood.appendChild(chips);
      node.appendChild(understood);
    }

    if (message.meetings.length) {
      node.classList.add("msg--wide");
      var cards = el("ul", "msg__cards");
      message.meetings.forEach(function (meeting) {
        cards.appendChild(renderCard(meeting, stale));
      });
      node.appendChild(cards);
    }

    if (message.create_url) {
      var create = el("a", "btn btn--soft btn--sm", "이 조건으로 직접 번개 열기");
      create.setAttribute("href", message.create_url);
      node.appendChild(create);
    }
    return node;
  }

  function scrollToEnd() {
    log.scrollTop = log.scrollHeight;
  }

  function addMessage(message) {
    messages.push(message);
    if (messages.length > MAX_STORED) messages = messages.slice(-MAX_STORED);
    saveMessages();
    log.appendChild(renderMessage(message, false));
    scrollToEnd();
  }

  function addError(reply) {
    addMessage({ role: "bot", reply: reply, mode: "", understood: [], meetings: [], create_url: null, error: true });
  }

  function showTyping() {
    typingNode = el("div", "msg msg--bot");
    var bubble = el("div", "bubble");
    var dots = el("span", "typing");
    dots.setAttribute("aria-hidden", "true");
    for (var i = 0; i < 3; i += 1) dots.appendChild(el("span", "typing__dot"));
    bubble.appendChild(dots);
    bubble.appendChild(el("span", "sr-only", "답변을 준비하고 있어요"));
    typingNode.appendChild(bubble);
    log.appendChild(typingNode);
    scrollToEnd();
  }

  function hideTyping() {
    if (typingNode && typingNode.parentNode) typingNode.parentNode.removeChild(typingNode);
    typingNode = null;
  }

  function setBusy(value) {
    busy = value;
    // 입력란은 잠그지 않는다. 잠그면 포커스가 풀려 휴대폰 화면 키보드가 내려가 버린다.
    // 요청 중에 또 보내는 것은 send()의 busy 검사와 보내기 버튼 잠금으로 막는다.
    sendButton.disabled = value;
    resetButton.disabled = value;
    if (quick) {
      quick.querySelectorAll("button").forEach(function (button) {
        button.disabled = value;
      });
    }
  }

  function updateStatus(left) {
    if (!statusLine || !aiEnabled) return;
    if (typeof left !== "number" || !isFinite(left)) return;
    statusLine.textContent = "AI 해석 오늘 " + Math.max(0, Math.floor(left)) + "회 남음";
  }

  // ---- 서버와 주고받기 ----
  function post(url, payload) {
    return fetch(url, {
      method: "POST",
      credentials: "same-origin",
      headers: {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "X-CSRFToken": csrfToken
      },
      body: JSON.stringify(payload || {})
    });
  }

  function goLogin() {
    window.location.href = loginUrl;
  }

  function send(raw) {
    if (busy) return;
    var message = String(raw || "").replace(/\s+/g, " ").trim();
    if (!message) return;
    if (message.length > maxMessage) {
      addError("메시지는 " + maxMessage + "자 안으로 적어 주세요.");
      return;
    }

    var typed = document.activeElement === input;
    addMessage({ role: "user", text: message });
    input.value = "";
    setBusy(true);
    showTyping();

    var leaving = false;
    post(apiUrl, { message: message })
      .then(function (response) {
        if (response.status === 401) {
          leaving = true;
          goLogin();
          return null;
        }
        return response.json().then(
          function (data) { return { ok: response.ok, status: response.status, data: data }; },
          function () { return { ok: false, status: response.status, data: null }; }
        );
      })
      .then(function (result) {
        if (!result) return;
        hideTyping();
        var data = result.data;
        if (!result.ok || !data || typeof data !== "object") {
          var reason = data && typeof data.error === "string" ? data.error : "";
          if (!reason) {
            reason = result.status === 400
              ? "요청이 만료됐어요. 페이지를 새로 고친 뒤 다시 시도해 주세요."
              : "지금은 답을 가져오지 못했어요. 잠시 뒤에 다시 시도해 주세요.";
          }
          addError(reason);
          return;
        }
        addMessage(normalizeBot(data));
        updateStatus(data.ai_left);
      })
      .catch(function () {
        hideTyping();
        addError("연결이 불안정해요. 인터넷 상태를 확인하고 다시 보내 주세요.");
      })
      .then(function () {
        if (leaving) return;
        hideTyping();
        setBusy(false);
        // 빠른 질문 칩을 눌렀을 때는 포커스를 주지 않는다. 키보드가 올라와 방금 나온 카드를 가린다.
        if (typed) input.focus();
      });
  }

  function resetChat() {
    if (busy) return;
    messages = [];
    clearMessages();
    Array.prototype.slice.call(log.children).forEach(function (child) {
      if (!child.hasAttribute("data-static")) log.removeChild(child);
    });
    input.value = "";
    // 초기화 응답이 오기 전에 질문을 보내면 이전 조건이 실린 쿠키가 다시 덮어쓴다. 끝날 때까지 전송을 막는다.
    setBusy(true);
    post(resetUrl, {})
      .then(function (response) {
        if (response.status === 401) goLogin();
      })
      .catch(function () {
        /* 화면은 이미 비웠다. 연결이 돌아오면 '새 대화'를 다시 누르면 된다. */
      })
      .then(function () {
        setBusy(false);
      });
  }

  // ---- 이벤트 연결 ----
  form.addEventListener("submit", function (event) {
    event.preventDefault();
    send(input.value);
  });

  if (quick) {
    quick.addEventListener("click", function (event) {
      var button = event.target.closest("[data-prompt]");
      if (!button || !quick.contains(button)) return;
      send(button.getAttribute("data-prompt"));
    });
  }

  resetButton.addEventListener("click", resetChat);

  // 모임을 보고 돌아왔을 때 이전 대화를 같은 방식으로 다시 그린다.
  messages = loadMessages();
  messages.forEach(function (message) {
    log.appendChild(renderMessage(message, true));
  });
  scrollToEnd();
})();
