// Security Patch Monitor notifier: posts native macOS notifications with the app's own name and icon.
//
//   notifier post [JSON]  Post one notification (JSON: id, title, subtitle, body, url, thread); JSON is read
//                         from the argument or, without one, from stdin.
//   notifier authorize    Ask for permission to show notifications (the system prompt appears only once);
//                         exits 0 when allowed, 3 when not.
//   notifier status FILE  Write the notification authorization status to FILE (for diagnostics).
//   notifier              Launched by macOS when a notification is clicked; opens its URL.
//
// Built into "Security Patch Monitor.app/Contents/Helpers/" by scripts/build_macos_app.sh. A separate app is
// needed because macOS attributes notifications to the bundle that posts them.

import AppKit
import UserNotifications

struct Payload: Decodable {
    let id: String
    let title: String
    let subtitle: String
    let body: String
    let url: String?
    let thread: String
}

let advisoryCategory = "advisory"
let openAction = "open-advisory"
let idleTimeout: TimeInterval = 20

func fail(_ message: String, code: Int32) -> Never {
    FileHandle.standardError.write(Data((message + "\n").utf8))
    exit(code)
}

final class NotificationHandler: NSObject, UNUserNotificationCenterDelegate {
    // Also show banners while this helper happens to be running.
    func userNotificationCenter(
        _ center: UNUserNotificationCenter,
        willPresent notification: UNNotification,
        withCompletionHandler completionHandler: @escaping (UNNotificationPresentationOptions) -> Void
    ) {
        completionHandler([.banner, .list, .sound])
    }

    // A click on the notification or on "Open advisory" opens the advisory in the browser.
    func userNotificationCenter(
        _ center: UNUserNotificationCenter,
        didReceive response: UNNotificationResponse,
        withCompletionHandler completionHandler: @escaping () -> Void
    ) {
        if response.actionIdentifier != UNNotificationDismissActionIdentifier,
           let link = response.notification.request.content.userInfo["url"] as? String,
           let url = URL(string: link), url.scheme == "https" {
            NSWorkspace.shared.open(url)
        }
        completionHandler()
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.5) { NSApp.terminate(nil) }
    }
}

func authorize(_ center: UNUserNotificationCenter, then action: @escaping () -> Void) {
    center.requestAuthorization(options: [.alert, .sound, .badge]) { granted, error in
        guard granted else {
            fail("Notifications not allowed: \(error?.localizedDescription ?? "denied in System Settings")", code: 3)
        }
        action()
    }
}

func post(_ payload: Payload, center: UNUserNotificationCenter) {
    authorize(center) {
        let content = UNMutableNotificationContent()
        content.title = payload.title
        content.subtitle = payload.subtitle
        content.body = payload.body
        content.threadIdentifier = payload.thread
        content.sound = .default
        if let url = payload.url {
            content.userInfo = ["url": url]
            content.categoryIdentifier = advisoryCategory  // "Open advisory" only when there is something to open
        }
        let request = UNNotificationRequest(identifier: payload.id, content: content, trigger: nil)
        center.add(request) { error in
            if let error {
                fail("Cannot post notification: \(error.localizedDescription)", code: 4)
            }
            exit(0)
        }
    }
}

let app = NSApplication.shared
// .accessory, not .prohibited: macOS silently denies the permission prompt to apps that can never show UI.
app.setActivationPolicy(.accessory)  // no Dock icon (LSUIElement), no menu bar

let handler = NotificationHandler()
let center = UNUserNotificationCenter.current()
center.delegate = handler
center.setNotificationCategories([
    UNNotificationCategory(
        identifier: advisoryCategory,
        actions: [UNNotificationAction(identifier: openAction, title: "Open advisory", options: [.foreground])],
        intentIdentifiers: [],
        options: []
    ),
])

let arguments = Array(CommandLine.arguments.dropFirst())
if arguments.first == "post" {
    let input = arguments.count > 1 ? Data(arguments[1].utf8) : FileHandle.standardInput.readDataToEndOfFile()
    guard let payload = try? JSONDecoder().decode(Payload.self, from: input) else {
        fail("Invalid notification payload", code: 2)
    }
    post(payload, center: center)
} else if arguments.first == "authorize" {
    authorize(center) { exit(0) }
} else if arguments.first == "status", arguments.count > 1 {
    center.getNotificationSettings { settings in
        let names: [UNAuthorizationStatus: String] = [
            .notDetermined: "notDetermined", .denied: "denied", .authorized: "authorized",
            .provisional: "provisional",
        ]
        let status = names[settings.authorizationStatus] ?? "unknown(\(settings.authorizationStatus.rawValue))"
        try? status.write(toFile: arguments[1], atomically: true, encoding: .utf8)
        exit(0)
    }
} else {
    // Launched by macOS to deliver a click: handle it, or quit when nothing arrives.
    DispatchQueue.main.asyncAfter(deadline: .now() + idleTimeout) { NSApp.terminate(nil) }
}
app.run()
