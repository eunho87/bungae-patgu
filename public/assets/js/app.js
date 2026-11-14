(function () {
  "use strict";

  // 되돌릴 수 없는 동작(모임 취소, 참여 취소, 댓글 삭제)은 한 번 더 묻는다.
  document.addEventListener("submit", function (event) {
    var form = event.target;
    if (!(form instanceof HTMLFormElement)) return;
    var message = form.getAttribute("data-confirm");
    if (message && !window.confirm(message)) {
      event.preventDefault();
      return;
    }
    // 응답이 늦을 때 버튼을 한 번 더 눌러 같은 모임이나 댓글이 둘 생기는 일을 막는다.
    // 검색 같은 GET 폼과, 스크립트가 따로 처리하는 챗봇 폼(action 없음)은 건드리지 않는다.
    if (event.defaultPrevented || form.method.toLowerCase() !== "post" || !form.getAttribute("action")) return;
    if (form.getAttribute("data-submitting")) {
      event.preventDefault();
      return;
    }
    form.setAttribute("data-submitting", "1");
    form.querySelectorAll("[type=submit]").forEach(function (button) {
      button.setAttribute("aria-disabled", "true");
      button.classList.add("is-busy");
    });
  });

  // 뒤로 가기로 돌아온 화면에서는 잠금을 푼다.
  window.addEventListener("pageshow", function () {
    document.querySelectorAll("form[data-submitting]").forEach(function (form) {
      form.removeAttribute("data-submitting");
      form.querySelectorAll(".is-busy").forEach(function (button) {
        button.removeAttribute("aria-disabled");
        button.classList.remove("is-busy");
      });
    });
  });

  // 넓은 화면에서는 접어 둘 이유가 없는 메뉴(홈의 날짜·시간대·정렬)를 펼쳐 둔다.
  if (window.matchMedia && window.matchMedia("(min-width: 960px)").matches) {
    document.querySelectorAll("details[data-open-wide]").forEach(function (details) {
      details.open = true;
    });
  }

  // 오류 문구를 해당 입력란과 연결해 화면 낭독기가 함께 읽게 한다.
  document.querySelectorAll(".field__error[id]").forEach(function (error) {
    var field = error.closest(".field");
    var control = field && field.querySelector("input:not([type=hidden]), select, textarea");
    if (!control) return;
    control.setAttribute("aria-invalid", "true");
    control.setAttribute("aria-describedby", error.id);
  });

  // 홈 필터: 칩이나 선택 상자를 바꾸면 바로 다시 검색한다. (스크립트가 꺼져 있으면 버튼으로 제출)
  document.querySelectorAll("form[data-autosubmit]").forEach(function (form) {
    function submitNow() {
      if (typeof form.requestSubmit === "function") {
        form.requestSubmit();
      } else {
        form.submit();
      }
    }

    form.addEventListener("change", function (event) {
      var target = event.target;
      if (target && target.matches("select, input[type=checkbox]")) submitNow();
    });

    // 종류 칩(라디오)은 직접 눌렀을 때만 제출한다. change로 하면 방향키로 옮길 때마다 화면이 새로 뜬다.
    // detail이 0이면 키보드로 생긴 클릭이므로 건너뛴다(그때는 검색 버튼으로 제출).
    var pending = false;
    form.addEventListener("click", function (event) {
      if (event.detail === 0 || pending) return;
      var label = event.target.closest ? event.target.closest("label") : null;
      if (!label || !label.querySelector("input[type=radio]")) return;
      pending = true;
      // 라디오가 실제로 선택된 다음에 제출되도록 한 박자 늦춘다.
      window.setTimeout(function () {
        // 제출 직전에 풀어 둔다. 풀지 않으면 결과 화면에서 '뒤로 가기'로 돌아왔을 때 칩이 더는 먹지 않는다.
        pending = false;
        submitNow();
      }, 0);
    });
  });

  // 빠른 선택 칩: data-set="date=2026-11-13;time=14:30"처럼 적힌 값을 해당 입력란(id)에 채운다.
  document.addEventListener("click", function (event) {
    var chip = event.target.closest ? event.target.closest("[data-set]") : null;
    if (!chip) return;
    chip.getAttribute("data-set").split(";").forEach(function (pair) {
      var cut = pair.indexOf("=");
      if (cut < 1) return;
      var field = document.getElementById(pair.slice(0, cut));
      if (!field) return;
      field.value = pair.slice(cut + 1);
      field.dispatchEvent(new Event("input", { bubbles: true }));
      field.dispatchEvent(new Event("change", { bubbles: true }));
    });
    var group = chip.parentNode;
    if (group) {
      group.querySelectorAll("[data-set]").forEach(function (other) {
        other.setAttribute("aria-pressed", other === chip ? "true" : "false");
      });
    }
  });

  // 모임 폼의 추천 태그 칩: 누르면 입력란에 넣고, 다시 누르면 뺀다.
  var TAG_SPLIT = /[,\s#]+/;

  function readTags(input) {
    var seen = [];
    input.value.split(TAG_SPLIT).forEach(function (token) {
      if (token && seen.indexOf(token) === -1) seen.push(token);
    });
    return seen;
  }

  document.querySelectorAll("[data-tag-picker]").forEach(function (picker) {
    var input = document.getElementById(picker.getAttribute("data-tag-input") || "");
    if (!input) return;
    var max = parseInt(picker.getAttribute("data-tag-max"), 10) || 5;
    var chips = picker.querySelectorAll("[data-tag]");

    function sync() {
      var tags = readTags(input);
      chips.forEach(function (chip) {
        var on = tags.indexOf(chip.getAttribute("data-tag")) !== -1;
        chip.setAttribute("aria-pressed", on ? "true" : "false");
        chip.disabled = !on && tags.length >= max;
      });
    }

    picker.addEventListener("click", function (event) {
      var chip = event.target.closest("[data-tag]");
      if (!chip || !picker.contains(chip)) return;
      var tag = chip.getAttribute("data-tag");
      var tags = readTags(input);
      var index = tags.indexOf(tag);
      if (index !== -1) {
        tags.splice(index, 1);
      } else if (tags.length < max) {
        tags.push(tag);
      }
      input.value = tags.join(" ");
      sync();
    });

    input.addEventListener("input", sync);
    sync();
  });

  // 링크 공유: 공유 시트를 쓸 수 있으면 그걸로, 아니면 주소를 복사한다.
  function legacyCopy(text) {
    var area = document.createElement("textarea");
    area.value = text;
    area.setAttribute("readonly", "");
    area.className = "sr-only";
    document.body.appendChild(area);
    area.select();
    var ok = false;
    try {
      ok = document.execCommand("copy");
    } catch (error) {
      ok = false;
    }
    document.body.removeChild(area);
    return ok;
  }

  // 클립보드 API가 권한 문제로 거절되는 브라우저(앱 안 브라우저 등)가 있어 예전 방식을 한 번 더 시도한다.
  function copyText(text) {
    if (navigator.clipboard && typeof navigator.clipboard.writeText === "function") {
      return navigator.clipboard.writeText(text).catch(function () {
        if (!legacyCopy(text)) throw new Error("copy failed");
      });
    }
    return legacyCopy(text) ? Promise.resolve() : Promise.reject(new Error("copy failed"));
  }

  document.querySelectorAll("[data-share-url]").forEach(function (button) {
    var original = button.textContent;
    var timer = null;

    function flash(text) {
      button.textContent = text;
      window.clearTimeout(timer);
      timer = window.setTimeout(function () {
        button.textContent = original;
      }, 2000);
    }

    button.addEventListener("click", function () {
      var url = button.getAttribute("data-share-url");
      var title = button.getAttribute("data-share-title") || "번개팟구";

      function copy() {
        copyText(url).then(
          function () { flash("링크를 복사했어요"); },
          // 자동 복사가 안 되면 주소를 보여 줘서 직접 복사할 수 있게 한다. 빈손으로 돌려보내지 않는다.
          function () { window.prompt("아래 주소를 복사해서 공유해 주세요.", url); }
        );
      }

      if (typeof navigator.share === "function") {
        navigator.share({ title: "[번개팟구] " + title, url: url }).catch(function (error) {
          // 사용자가 공유 창을 닫은 경우는 그대로 둔다.
          if (error && error.name === "AbortError") return;
          copy();
        });
        return;
      }
      copy();
    });
  });
})();
