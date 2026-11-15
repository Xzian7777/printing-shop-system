#include <SPI.h>
#include <MFRC522.h>
#include <Preferences.h>

// Matches the connected ESP32 + RC522 wiring.
constexpr uint8_t SS_PIN = 5;
constexpr uint8_t SCK_PIN = 18;
constexpr uint8_t MOSI_PIN = 23;
constexpr uint8_t MISO_PIN = 19;
constexpr uint8_t RST_PIN = 0;
constexpr uint8_t QUEUE_SIZE = 32;
constexpr unsigned long REMOVAL_MS = 600;

MFRC522 reader(SS_PIN, RST_PIN);
Preferences storage;
String readerId;
String commands;
String queued[QUEUE_SIZE];
uint64_t nextSequence = 1;
bool held = false;
unsigned long absentSince = 0;
unsigned long lastPoll = 0;
unsigned long lastSend = 0;
unsigned long lastHello = 0;

String queueKey(uint8_t index) {
  char key[5];
  snprintf(key, sizeof(key), "q%02u", index);
  return String(key);
}

void hello() {
  byte version = reader.PCD_ReadRegister(MFRC522::VersionReg);
  Serial.printf("{\"type\":\"hello\",\"protocol\":1,\"readerId\":\"%s\",\"readerVersion\":%u}\n", readerId.c_str(), version);
}

void acknowledge(const String &eventId) {
  for (uint8_t index = 0; index < QUEUE_SIZE; index++) {
    if (queued[index].startsWith(eventId + "|")) {
      storage.remove(queueKey(index).c_str());
      queued[index] = "";
      return;
    }
  }
}

void readCommands() {
  while (Serial.available()) {
    char value = Serial.read();
    if (value == '\n') {
      commands.trim();
      if (commands.startsWith("ACK ")) acknowledge(commands.substring(4));
      if (commands == "PING") hello();
      commands = "";
    } else if (commands.length() < 128) {
      commands += value;
    } else {
      commands = "";
    }
  }
}

bool persistTap(const String &uid) {
  for (uint8_t index = 0; index < QUEUE_SIZE; index++) {
    if (queued[index].length()) continue;
    String eventId = readerId + "-" + String((unsigned long long)nextSequence);
    String record = eventId + "|" + uid;
    if (storage.putString(queueKey(index).c_str(), record) == 0) return false;
    queued[index] = record;
    nextSequence++;
    storage.putULong64("next", nextSequence);
    return true;
  }
  return false;
}

void sendPending() {
  // Sequence order survives NVS slot reuse and reconnects.
  int selected = -1;
  uint64_t smallest = UINT64_MAX;
  for (uint8_t index = 0; index < QUEUE_SIZE; index++) {
    if (!queued[index].length()) continue;
    int separator = queued[index].indexOf('|');
    uint64_t sequence = strtoull(queued[index].substring(readerId.length() + 1, separator).c_str(), nullptr, 10);
    if (sequence < smallest) { smallest = sequence; selected = index; }
  }
  if (selected < 0) return;
  int separator = queued[selected].indexOf('|');
  String eventId = queued[selected].substring(0, separator);
  String uid = queued[selected].substring(separator + 1);
  Serial.printf("{\"type\":\"tap\",\"readerId\":\"%s\",\"eventId\":\"%s\",\"cardUid\":\"%s\"}\n",
                readerId.c_str(), eventId.c_str(), uid.c_str());
}

void setup() {
  Serial.begin(115200);
  uint64_t mac = ESP.getEfuseMac();
  char identifier[13];
  snprintf(identifier, sizeof(identifier), "%04X%08X", (unsigned int)(mac >> 32), (unsigned int)mac);
  readerId = identifier;
  storage.begin("kcz-rfid", false);
  nextSequence = storage.getULong64("next", 1);
  held = storage.getBool("held", false);
  for (uint8_t index = 0; index < QUEUE_SIZE; index++) {
    queued[index] = storage.getString(queueKey(index).c_str(), "");
    if (!queued[index].length()) continue;
    held = true;
    int separator = queued[index].indexOf('|');
    uint64_t sequence = strtoull(queued[index].substring(readerId.length() + 1, separator).c_str(), nullptr, 10);
    if (sequence >= nextSequence) nextSequence = sequence + 1;
  }
  storage.putULong64("next", nextSequence);
  storage.putBool("held", held);
  SPI.begin(SCK_PIN, MISO_PIN, MOSI_PIN, SS_PIN);
  reader.PCD_Init();
  delay(10);
  byte version = reader.PCD_ReadRegister(MFRC522::VersionReg);
  if (version == 0 || version == 0xFF) {
    Serial.println("{\"type\":\"error\",\"message\":\"RC522 not detected. Check power and wiring.\"}");
  }
  hello();
}

void loop() {
  readCommands();
  unsigned long now = millis();
  if (now - lastHello >= 5000) { hello(); lastHello = now; }
  if (now - lastSend >= 700) { sendPending(); lastSend = now; }
  if (now - lastPoll < 100) return;
  lastPoll = now;
  byte answer[2];
  byte size = sizeof(answer);
  MFRC522::StatusCode status = reader.PICC_WakeupA(answer, &size);
  bool present = status == MFRC522::STATUS_OK || status == MFRC522::STATUS_COLLISION;
  if (!present) {
    if (absentSince == 0) absentSince = now;
    if (held && now - absentSince >= REMOVAL_MS) {
      held = false;
      storage.putBool("held", false);
    }
    return;
  }
  absentSince = 0;
  if (!reader.PICC_ReadCardSerial()) return;
  if (!held) {
    String uid;
    for (byte index = 0; index < reader.uid.size; index++) {
      char hex[3];
      snprintf(hex, sizeof(hex), "%02X", reader.uid.uidByte[index]);
      uid += hex;
    }
    if (!persistTap(uid)) {
      Serial.println("{\"type\":\"error\",\"message\":\"Reader queue full. Reconnect the bridge, then remove and tap again.\"}");
    }
    held = true;
    storage.putBool("held", true);
  }
  reader.PICC_HaltA();
  reader.PCD_StopCrypto1();
}
