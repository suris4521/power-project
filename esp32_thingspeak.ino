#include <WiFi.h>
#include <HTTPClient.h>
#include <WebServer.h>

// ---- User config ----
const char* WIFI_SSID = "suri";
const char* WIFI_PASS = "12345678";
const String TS_WRITE_KEY = "JS4KE9YYJ5C8TIBW";
const String TS_READ_KEY  = "PLHADOBANAWGG825";
const int TS_CHANNEL_ID   = 3211996;  // your channel
const float COST_PER_KWH = 0.12f;
const float VOLT_ALERT = 10.0f;
// Sensor calibration
const int VOLT_PIN = 34;
const int CURR_PIN = 35;
const float ADC_REF = 3.3f;
const float ADC_COUNTS = 4095.0f;
const float VOLT_DIVIDER_RATIO = 11.0f;
const float CURRENT_MV_PER_A = 66.0f;
const float CURRENT_ZERO_MV = 2500.0f;
const int ADC_SAMPLES = 20;
// ---------------------

const int RELAY_PIN = 5;
const int ALERT_LED = 2;
const int BUZZER_PIN = 4;

unsigned long lastPost = 0;
float energy_Wh = 0.0f;
unsigned long lastLoopMs = 0;
int relayState = 0;
WebServer server(80);

float readVoltage() {
  uint32_t acc = 0;
  for (int i = 0; i < ADC_SAMPLES; i++) {
    acc += analogRead(VOLT_PIN);
    delayMicroseconds(500);
  }
  float adc = acc / (float)ADC_SAMPLES;
  float sensed = (adc / ADC_COUNTS) * ADC_REF;
  float lineV = sensed * VOLT_DIVIDER_RATIO;
  return lineV;
}

float readCurrent() {
  uint32_t acc = 0;
  for (int i = 0; i < ADC_SAMPLES; i++) {
    acc += analogRead(CURR_PIN);
    delayMicroseconds(500);
  }
  float adc = acc / (float)ADC_SAMPLES;
  float mv = (adc / ADC_COUNTS) * ADC_REF * 1000.0f;
  float mv_from_zero = mv - CURRENT_ZERO_MV;
  float amps = mv_from_zero / CURRENT_MV_PER_A;
  return amps;
}

void buzzAlert() { tone(BUZZER_PIN, 2000, 400); }

void connectWifi() {
  WiFi.begin(WIFI_SSID, WIFI_PASS);
  Serial.print("Connecting WiFi");
  while (WiFi.status() != WL_CONNECTED) {
    delay(500);
    Serial.print(".");
  }
  Serial.println("\nWiFi connected");
  Serial.print("IP address: ");
  Serial.println(WiFi.localIP());
}

void handleRelay() {
  if (!server.hasArg("cmd")) {
    server.send(400, "text/plain", "missing cmd");
    return;
  }
  int cmd = server.arg("cmd").toInt();
  relayState = (cmd == 1) ? 1 : 0;
  digitalWrite(RELAY_PIN, relayState ? HIGH : LOW);
  Serial.printf("Relay cmd=%d (from Flask)\n", relayState);
  server.send(200, "application/json", String("{\"relay\":") + relayState + "}");
}

void handlePing() {
  server.send(200, "text/plain", "ok");
}

void setup() {
  Serial.begin(115200);
  pinMode(RELAY_PIN, OUTPUT);
  pinMode(ALERT_LED, OUTPUT);
  pinMode(BUZZER_PIN, OUTPUT);
  pinMode(VOLT_PIN, INPUT);
  pinMode(CURR_PIN, INPUT);
  analogReadResolution(12);
  analogSetAttenuation(ADC_11db);
  digitalWrite(RELAY_PIN, LOW);
  connectWifi();

  server.on("/relay", handleRelay);
  server.on("/ping", handlePing);
  server.begin();
  Serial.println("HTTP server started");

  lastLoopMs = millis();
}

void loop() {
  if (WiFi.status() != WL_CONNECTED) {
    connectWifi();
    delay(2000);
    return;
  }

  // handle incoming HTTP commands from Flask
  server.handleClient();

  float v = readVoltage();
  float i = readCurrent();
  float p = v * i;

  unsigned long now = millis();
  float dt_hours = (now - lastLoopMs) / 3600000.0f;
  energy_Wh += p * dt_hours;
  lastLoopMs = now;

  float energy_kWh = energy_Wh / 1000.0f;
  float cost = energy_kWh * COST_PER_KWH;

  bool highV = v > VOLT_ALERT;
  digitalWrite(ALERT_LED, highV ? HIGH : LOW);
  if (highV) buzzAlert();

  // Push data to ThingSpeak (15s limit)
  if (millis() - lastPost > 15000) {
    HTTPClient http;
    String url = "http://api.thingspeak.com/update?api_key=" + TS_WRITE_KEY +
                 "&field1=" + String(v, 2) +
                 "&field2=" + String(i, 2) +
                 "&field3=" + String(p, 2) +
                 "&field4=" + String(energy_kWh, 4) +
                 "&field6=" + String(highV ? 1 : 0) +
                 "&field7=" + String(cost, 4);
    http.begin(url);
    int code = http.GET();
    Serial.printf("POST TS code=%d V=%.2f I=%.2f P=%.2f E=%.4fkWh Cost=%.4f\n",
                  code, v, i, p, energy_kWh, cost);
    http.end();
    lastPost = millis();
  }

  delay(1000);
}