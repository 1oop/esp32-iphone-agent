import CoreBluetooth
import Foundation

/// CoreBluetooth central for the board: scan, connect (bonding happens on the first `net` read), command
/// writes, status / reply / video notifies. Reconnects on its own; restores state when iOS relaunches the app.
@MainActor final class BoardLink: NSObject, ObservableObject, BoardTransport {
    struct Found: Identifiable, Equatable {
        let id: UUID
        let name: String
        let rssi: Int
    }

    @Published private(set) var state: CBManagerState = .unknown
    @Published private(set) var found: [Found] = []
    @Published private(set) var scanning = false
    @Published private(set) var connected = false  // subscribed and ready for commands
    @Published private(set) var linking = false    // connected over the air, pairing / subscribing
    @Published private(set) var pairedID: UUID?
    @Published private(set) var name = ""
    @Published private(set) var status: BoardStatus?
    @Published private(set) var net: NetConfig?
    @Published private(set) var lastError = ""

    let commands = CommandChannel()
    var onVideo: ((Data) -> Void)?
    var onStatus: ((BoardStatus) -> Void)?
    var onLinkUp: (() -> Void)?
    var onLinkDown: (() -> Void)?
    private var lastNotify = 0.0  // last notification of any kind, to tell a supervision timeout from a peer drop
    private var readySince = 0.0
    var log: (String) -> Void = { print($0) }

    private var central: CBCentralManager!
    private var peripheral: CBPeripheral?
    private var chars: [CBUUID: CBCharacteristic] = [:]
    private var subscribed = Set<CBUUID>()
    private var netWaiters: [CheckedContinuation<NetConfig, Error>] = []
    private static let pairedKey = "pairedBoard"
    private var statusReads: Task<Void, Never>?
    let mock: Bool  // demo data: no Bluetooth, every command answers ok (MockMode)

    init(mock: Bool = false) {
        self.mock = mock
        super.init()
        pairedID = UserDefaults.standard.string(forKey: Self.pairedKey).flatMap(UUID.init)
        commands.transport = self
        if mock { return }
        central = CBCentralManager(delegate: self, queue: .main, options: [
            CBCentralManagerOptionRestoreIdentifierKey: "iphone-agent-central",
            CBCentralManagerOptionShowPowerAlertKey: true,
        ])
    }

    var isConnected: Bool { connected }
    var mtu: Int { peripheral.map { $0.maximumWriteValueLength(for: .withoutResponse) + 3 } ?? 0 }

    // MARK: - scan / pair

    func scan() {
        guard state == .poweredOn, !mock else { return }
        found = []
        scanning = true
        central.scanForPeripherals(withServices: [BoardUUID.service], options: nil)
    }

    func stopScan() {
        central?.stopScan()
        scanning = false
    }

    /// Connect to a board and remember it; reading `net` then triggers the iOS pairing prompt.
    func pair(_ id: UUID) {
        if mock { return mockPair(id) }
        stopScan()
        pairedID = id
        UserDefaults.standard.set(id.uuidString, forKey: Self.pairedKey)
        reconnect()
    }

    /// Ask the board to erase its bonds, then forget it here. iOS keeps its own bond: the user removes
    /// "ESP32 Agent" in Settings > Bluetooth (apps cannot do that).
    func unpair() async {
        _ = try? await commands.send("unpair")
        forget()
    }

    /// Forget the board in the app only.
    func forget() {
        if let peripheral { central?.cancelPeripheralConnection(peripheral) }
        peripheral = nil
        pairedID = nil
        UserDefaults.standard.removeObject(forKey: Self.pairedKey)
        setDisconnected()
    }

    private func reconnect() {
        guard state == .poweredOn, !mock, let id = pairedID else { return }
        if peripheral?.identifier != id {
            peripheral = central.retrievePeripherals(withIdentifiers: [id]).first
        }
        guard let peripheral else {  // iOS does not know it (yet): find it by scanning, then connect
            lastError = "board not found; scanning"
            if !scanning { scan() }
            return
        }
        peripheral.delegate = self
        name = peripheral.name ?? name
        central.connect(peripheral, options: nil)  // no timeout: completes whenever the board shows up
    }

    // MARK: - net

    /// Read the network config (the first read pairs the phone with the board).
    func readNet() async throws -> NetConfig {
        guard let peripheral, let c = chars[BoardUUID.net] else { throw DeviceError(message: "board not connected") }
        return try await withCheckedThrowingContinuation { cont in
            netWaiters.append(cont)
            peripheral.readValue(for: c)
        }
    }

    func writeNet(_ config: NetConfig) throws {
        guard let peripheral, let c = chars[BoardUUID.net] else { throw DeviceError(message: "board not connected") }
        peripheral.writeValue(try JSONEncoder().encode(config), for: c, type: .withResponse)
    }

    // MARK: - BoardTransport

    func write(_ line: String) throws {
        if mock, connected { return mockReply(line) }
        guard let peripheral, let c = chars[BoardUUID.cmd], connected else {
            throw DeviceError(message: "board not connected")
        }
        let data = Data(line.utf8)
        let fast = data.count <= peripheral.maximumWriteValueLength(for: .withoutResponse)
            && c.properties.contains(.writeWithoutResponse) && peripheral.canSendWriteWithoutResponse
        peripheral.writeValue(data, for: c, type: fast ? .withoutResponse : .withResponse)  // long writes need a response
    }

    var maxWrite: Int { peripheral?.maximumWriteValueLength(for: .withoutResponse) ?? 20 }

    func writeIfIdle(_ line: String) -> Bool {
        guard !mock, let peripheral, let c = chars[BoardUUID.cmd], connected,
              c.properties.contains(.writeWithoutResponse), peripheral.canSendWriteWithoutResponse else { return false }
        let data = Data(line.utf8)
        guard data.count <= peripheral.maximumWriteValueLength(for: .withoutResponse) else { return false }
        peripheral.writeValue(data, for: c, type: .withoutResponse)
        return true
    }

    func dropLink() {
        guard let peripheral, !mock else { return }
        central.cancelPeripheralConnection(peripheral)  // didDisconnect reconnects
    }

    // MARK: - demo data

    /// Demo onboarding: Bluetooth is on and one board is nearby, not paired yet.
    func mockFound() {
        state = .poweredOn
        pairedID = nil
        found = [Found(id: UUID(), name: "ESP32 Agent 7C3E", rssi: -58)]
    }

    private func mockPair(_ id: UUID) {
        scanning = true
        Task { @MainActor in
            try? await Task.sleep(nanoseconds: 1_200_000_000)
            scanning = false
            mockConnect(name: "ESP32 Agent 7C3E",
                        net: NetConfig(mode: "ap", ssid: "ESP32Agent-7C3E", pass: "demo-pass", ip: "192.168.4.1"))
        }
    }

    /// Pretend a board is paired and connected (MockMode).
    func mockConnect(name: String, net: NetConfig) {
        state = .poweredOn
        self.name = name
        self.net = net
        pairedID = pairedID ?? UUID()
        found = [Found(id: UUID(), name: "ESP32 Agent 7C3E", rssi: -71)]
        connected = true
        commands.linkUp()
        onLinkUp?()
    }

    func mockStatus(_ s: BoardStatus) {
        status = s
        onStatus?(s)
    }

    private func mockReply(_ line: String) {
        let parts = line.split(separator: " ").map(String.init)
        let extra = parts.count > 1 && parts[1] == "bench" ? " 6400" : ""
        Task { @MainActor in
            try? await Task.sleep(nanoseconds: 40_000_000)
            commands.handleReply("\(parts[0]) ok" + extra)
        }
    }

    /// Notifies may be shortened on a small MTU; read the full status every 5 s and merge it. The board notifies
    /// its status every second, so 5 s of silence (while this loop kept running) means it was unplugged: drop the
    /// link now instead of waiting for the Bluetooth timeout; the pending connect picks it up when it is back.
    private func startFullStatusReads() {
        statusReads?.cancel()
        statusReads = Task { [weak self] in
            var tick = 0, lastTick = now()
            while !Task.isCancelled {
                guard let self, self.connected, let p = self.peripheral, let c = self.chars[BoardUUID.status] else { return }
                if tick % 5 == 0 { p.readValue(for: c) }
                if now() - lastTick < 2 && now() - self.lastNotify > 5 {
                    DebugLog.write(String(format: "BLE: no notification for %.0f s; dropping the link", now() - self.lastNotify))
                    return self.dropLink()
                }
                tick += 1
                lastTick = now()
                try? await Task.sleep(nanoseconds: 1_000_000_000)
            }
        }
    }

    private func setDisconnected() {
        statusReads?.cancel()
        let was = connected
        connected = false
        linking = false
        chars = [:]
        subscribed = []
        commands.linkDown()
        netWaiters.forEach { $0.resume(throwing: DeviceError(message: "board disconnected")) }
        netWaiters = []
        if was {
            log("board disconnected")
            onLinkDown?()
        }
    }
}

extension BoardLink: CBCentralManagerDelegate {
    nonisolated func centralManagerDidUpdateState(_ central: CBCentralManager) {
        MainActor.assumeIsolated {
            state = central.state
            if central.state == .poweredOn { reconnect() } else { setDisconnected() }
        }
    }

    nonisolated func centralManager(_ central: CBCentralManager, willRestoreState dict: [String: Any]) {
        MainActor.assumeIsolated {
            if let restored = (dict[CBCentralManagerRestoredStatePeripheralsKey] as? [CBPeripheral])?.first {
                peripheral = restored
                restored.delegate = self
                log("restored board link after relaunch")
            }
        }
    }

    nonisolated func centralManager(_ central: CBCentralManager, didDiscover p: CBPeripheral,
                                    advertisementData: [String: Any], rssi: NSNumber) {
        MainActor.assumeIsolated {
            let name = advertisementData[CBAdvertisementDataLocalNameKey] as? String ?? p.name ?? "ESP32 Agent"
            let item = Found(id: p.identifier, name: name, rssi: rssi.intValue)
            if let i = found.firstIndex(where: { $0.id == item.id }) { found[i] = item } else { found.append(item) }
            if p.identifier == pairedID && !connected && !linking {  // the paired board is back: connect at once
                stopScan()
                peripheral = p
                reconnect()
            }
        }
    }

    nonisolated func centralManager(_ central: CBCentralManager, didConnect p: CBPeripheral) {
        MainActor.assumeIsolated {
            name = p.name ?? name
            linking = true
            log("board connected, discovering")
            p.discoverServices([BoardUUID.service])
        }
    }

    nonisolated func centralManager(_ central: CBCentralManager, didFailToConnect p: CBPeripheral, error: Error?) {
        MainActor.assumeIsolated {
            lastError = error?.localizedDescription ?? "connect failed"
            let e = error as NSError?
            DebugLog.write("BLE connect failed: \(e?.domain ?? "-") \(e?.code ?? 0): \(lastError)")
            reconnect()
        }
    }

    nonisolated func centralManager(_ central: CBCentralManager, didDisconnectPeripheral p: CBPeripheral,
                                    error: Error?) {
        MainActor.assumeIsolated {
            let e = error as NSError?
            DebugLog.write(String(format: "BLE disconnect: %@ %d: %@; last notification %.1f s ago; link up %.0f s",
                                  e?.domain ?? "no error", e?.code ?? 0, e?.localizedDescription ?? "-",
                                  lastNotify > 0 ? now() - lastNotify : -1, readySince > 0 ? now() - readySince : -1))
            setDisconnected()
            if let error { lastError = error.localizedDescription }
            reconnect()  // pending connect: iOS completes it when the board is back, even in background
        }
    }
}

extension BoardLink: CBPeripheralDelegate {
    nonisolated func peripheral(_ p: CBPeripheral, didDiscoverServices error: Error?) {
        MainActor.assumeIsolated {
            guard let service = p.services?.first(where: { $0.uuid == BoardUUID.service }) else {
                lastError = "board service missing"
                return
            }
            p.discoverCharacteristics(BoardUUID.all, for: service)
        }
    }

    nonisolated func peripheral(_ p: CBPeripheral, didDiscoverCharacteristicsFor service: CBService, error: Error?) {
        MainActor.assumeIsolated {
            for c in service.characteristics ?? [] {
                chars[c.uuid] = c
                if [BoardUUID.reply, BoardUUID.status, BoardUUID.video].contains(c.uuid) { p.setNotifyValue(true, for: c) }
            }
        }
    }

    nonisolated func peripheral(_ p: CBPeripheral, didUpdateNotificationStateFor c: CBCharacteristic, error: Error?) {
        MainActor.assumeIsolated {
            if let error { lastError = "subscribe \(c.uuid): \(error.localizedDescription)"; return }
            subscribed.insert(c.uuid)
            if !connected && subscribed.isSuperset(of: [BoardUUID.reply, BoardUUID.status, BoardUUID.video]) {
                connected = true
                linking = false
                startFullStatusReads()
                commands.linkUp()
                readySince = now()
                lastNotify = now()
                onLinkUp?()
                log("board ready (MTU \(mtu))")
            }
        }
    }

    nonisolated func peripheral(_ p: CBPeripheral, didUpdateValueFor c: CBCharacteristic, error: Error?) {
        MainActor.assumeIsolated {
            lastNotify = now()
            if c.uuid == BoardUUID.net {
                let waiters = netWaiters
                netWaiters = []
                if let error {
                    waiters.forEach { $0.resume(throwing: error) }
                } else if let v = c.value, let config = try? JSONDecoder().decode(NetConfig.self, from: v) {
                    net = config
                    waiters.forEach { $0.resume(returning: config) }
                } else {
                    waiters.forEach { $0.resume(throwing: DeviceError(message: "bad net config")) }
                }
                return
            }
            guard error == nil, let v = c.value else { return }
            switch c.uuid {
            case BoardUUID.video: onVideo?(v)
            case BoardUUID.reply: commands.handleReply(String(decoding: v, as: UTF8.self))
            case BoardUUID.status:
                if let s = BoardStatus.decode(v) {
                    let merged = status?.merged(with: s) ?? s
                    status = merged
                    onStatus?(merged)
                }
            default: break
            }
        }
    }
}
