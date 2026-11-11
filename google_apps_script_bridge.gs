/**
 * ============================================================================
 * 일쩜오도(1.5°) 재고현황 | Google Drive 영구 저장 브리지 (Google Apps Script)
 * ============================================================================
 * [설치 방법 - 약 3분 소요]
 * 1. 회사 Google Drive (drive.google.com) 접속 → [+ 새로 만들기] → [더보기] → [Google Apps Script]
 * 2. 편집기 기존 코드를 모두 지우고 아래 전체 코드를 그대로 붙여넣습니다.
 * 3. 아래 API_TOKEN 값을 본인만의 비밀 문자열(예: "iljeomodo-secret-2026")로 바꿉니다.
 * 4. 상단 [저장(Ctrl+S)] 클릭 → 우측 상단 [배포] → [새 배포] 클릭
 *    - 유형 선택: [웹 앱]
 *    - 다음 사용자 권한으로 실행: [나(대표님 계정)]
 *    - 액세스 권한이 있는 사용자: [모든 사용자]  <-- Render 서버가 통신할 수 있도록 반드시 선택!
 * 5. [배포] 클릭 후 권한 승인 진행 → 생성된 [웹 앱 URL] (https://script.google.com/macros/s/.../exec) 복사
 * 6. Render 대시보드 → [Environment] 탭에 아래 환경변수 추가 후 저장:
 *    - GOOGLE_APPS_SCRIPT_URL = 복사한 웹 앱 URL
 *    - GOOGLE_APPS_SCRIPT_TOKEN = 위에서 설정한 API_TOKEN 값
 *    - ADMIN_PIN = 원하는 관리자 PIN 번호 (예: 1500)
 * ============================================================================
 */

const DB_FOLDER_NAME = "일쩜오도_재고현황_DB";
const DB_FILE_NAME = "iljeomodo_inventory_db.json";

// ⚠️ Render 환경변수 GOOGLE_APPS_SCRIPT_TOKEN 과 반드시 동일하게 맞춰주세요!
const API_TOKEN = "iljeomodo-drive-secret-token";

function doGet(e) {
  return jsonResponse({
    success: true,
    service: "iljeomodo-drive-bridge",
    message: "일쩜오도 Google Drive 영구 저장 브리지가 정상 가동 중입니다."
  });
}

function doPost(e) {
  var lock = LockService.getScriptLock();
  try {
    lock.waitLock(15000);

    var rawBody = (e && e.postData && e.postData.contents) ? e.postData.contents : "{}";
    var body = JSON.parse(rawBody);

    if (!body.token || body.token !== API_TOKEN) {
      return jsonResponse({ success: false, error: "Unauthorized: Invalid token" });
    }

    var action = body.action || "ping";
    if (action === "ping") {
      return jsonResponse({
        success: true,
        message: "pong",
        folderName: DB_FOLDER_NAME,
        fileName: DB_FILE_NAME
      });
    }

    var file = findDbFile();

    if (action === "load") {
      if (!file) {
        return jsonResponse({
          success: true,
          exists: false,
          data: null
        });
      }
      var content = file.getBlob().getDataAsString("UTF-8");
      var parsed = content ? JSON.parse(content) : {};
      return jsonResponse({
        success: true,
        exists: true,
        fileId: file.getId(),
        updatedAt: file.getLastUpdated().toISOString(),
        data: parsed
      });
    }

    if (action === "save") {
      if (
        !body.payload ||
        typeof body.payload !== "object" ||
        Array.isArray(body.payload) ||
        !Array.isArray(body.payload.master_products) ||
        (body.payload.master_products.length === 0 && !body.force)
      ) {
        return jsonResponse({ success: false, error: "Invalid or empty payload object (master_products missing)" });
      }
      var jsonStr = JSON.stringify(body.payload, null, 2);
      var targetFolder = null;
      if (!file) {
        targetFolder = findOrCreateFolder();
        var blob = Utilities.newBlob(jsonStr, "application/json", DB_FILE_NAME);
        file = targetFolder.createFile(blob);
      } else {
        // 일일 1회(KST 기준) 덮어쓰기 전 안전 백업 스냅샷 생성 (최근 30일 보관)
        try {
          var prevContent = file.getBlob().getDataAsString("UTF-8");
          if (prevContent && prevContent.length > 2) {
            var kstToday = Utilities.formatDate(new Date(), "GMT+9", "yyyy-MM-dd");
            var backupName = "iljeomodo_backup_" + kstToday + ".json";
            var parents = file.getParents();
            targetFolder = parents.hasNext() ? parents.next() : findOrCreateFolder();
            if (!targetFolder.getFilesByName(backupName).hasNext()) {
              targetFolder.createFile(Utilities.newBlob(prevContent, "application/json", backupName));
              pruneOldDailyBackups(targetFolder, 30);
            }
          }
        } catch (bakErr) {}
        file.setContent(jsonStr);
      }
      return jsonResponse({
        success: true,
        fileId: file.getId(),
        fileName: file.getName(),
        updatedAt: new Date().toISOString()
      });
    }

    return jsonResponse({ success: false, error: "Unknown action: " + action });
  } catch (err) {
    return jsonResponse({ success: false, error: String(err) });
  } finally {
    try {
      lock.releaseLock();
    } catch (ignore) {}
  }
}

function findOrCreateFolder() {
  var folders = DriveApp.getFoldersByName(DB_FOLDER_NAME);
  if (folders.hasNext()) {
    return folders.next();
  }
  return DriveApp.createFolder(DB_FOLDER_NAME);
}

function findDbFile() {
  var folders = DriveApp.getFoldersByName(DB_FOLDER_NAME);
  if (folders.hasNext()) {
    var folder = folders.next();
    var files = folder.getFilesByName(DB_FILE_NAME);
    if (files.hasNext()) {
      return files.next();
    }
  }
  // 폴더 외부 루트 등에서 동일 이름 파일 탐색 대비
  var rootFiles = DriveApp.getFilesByName(DB_FILE_NAME);
  if (rootFiles.hasNext()) {
    return rootFiles.next();
  }
  return null;
}

function pruneOldDailyBackups(folder, maxKeep) {
  var backups = [];
  var files = folder.getFiles();
  while (files.hasNext()) {
    var f = files.next();
    if (/^iljeomodo_backup_\d{4}-\d{2}-\d{2}\.json$/.test(f.getName())) {
      backups.push(f);
    }
  }
  if (backups.length <= maxKeep) {
    return;
  }
  backups.sort(function(a, b) {
    return a.getName() < b.getName() ? 1 : (a.getName() > b.getName() ? -1 : 0);
  });
  for (var i = maxKeep; i < backups.length; i++) {
    try {
      backups[i].setTrashed(true);
    } catch (ignore) {}
  }
}

function jsonResponse(obj) {
  return ContentService
    .createTextOutput(JSON.stringify(obj))
    .setMimeType(ContentService.MimeType.JSON);
}

