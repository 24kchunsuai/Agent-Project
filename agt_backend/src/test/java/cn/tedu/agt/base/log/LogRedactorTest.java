package cn.tedu.agt.base.log;

import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Test;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * 日志脱敏（E-6 / S-8）。
 *
 * <p>这批用例守的不是"功能"，而是<strong>不该漏的东西真的不漏</strong>：
 * 脱敏漏一次，密钥就永久留在磁盘上（还会被轮转带进历史文件）。</p>
 *
 * <p>{@link #authorizationBearerIsFullyMasked()} 是一条<strong>真抓到过缺陷</strong>的用例：
 * 最初 {@code Authorization} 规则排在 {@code Bearer} 规则前面，
 * 于是 {@code "Authorization: Bearer eyJ..."} 里的值 "Bearer" 先被替掉，
 * 真正的 token 原样留在日志里——顺序一改就对了。</p>
 */
class LogRedactorTest {

    @AfterEach
    void tearDown() {
        org.slf4j.MDC.clear();
    }

    @Test
    @DisplayName("登记过的密钥字面量：无论用什么措辞出现，都被替换")
    void registeredSecretIsMasked() {
        LogRedactor.registerSecret("unit-test-secret-value-1234567890");

        String out = LogRedactor.redact("启动参数 shared=unit-test-secret-value-1234567890 完毕");

        assertThat(out).doesNotContain("unit-test-secret-value-1234567890");
        assertThat(out).contains("***");
    }

    @Test
    @DisplayName("Authorization: Bearer <jwt> → token 本体必须消失（顺序敏感的用例）")
    void authorizationBearerIsFullyMasked() {
        String out = LogRedactor.redact("Authorization: Bearer eyJhbGciOiJIUzI1NiJ9");

        assertThat(out).doesNotContain("eyJhbGciOiJIUzI1NiJ9");
    }

    @Test
    @DisplayName("X-Agent-Key 与 AGENT_SHARED_KEY 的值被替换")
    void agentKeyValueIsMasked() {
        assertThat(LogRedactor.redact("X-Agent-Key: 7f3ac9d2e1b84f60"))
                .doesNotContain("7f3ac9d2e1b84f60");
        assertThat(LogRedactor.redact("AGENT_SHARED_KEY=abcdef1234567890"))
                .doesNotContain("abcdef1234567890");
    }

    @Test
    @DisplayName("第三方密钥形态 sk-/pk- 被替换")
    void thirdPartyKeyIsMasked() {
        assertThat(LogRedactor.redact("llm key sk-abcdef0123456789 rejected"))
                .doesNotContain("sk-abcdef0123456789");
    }

    @Test
    @DisplayName("JSON 里的 apiKey / password 字段值被替换")
    void jsonSecretFieldsAreMasked() {
        String out = LogRedactor.redact("{\"apiKey\":\"sk-abcdef0123456789\",\"user\":\"u1\"}");

        assertThat(out).doesNotContain("sk-abcdef0123456789");
        assertThat(out).contains("u1");                 // 只脱敏，不误伤其它字段
    }

    @Test
    @DisplayName("已掩码形态 ab***yz(len=43) 不被二次破坏（否则再也看不出密钥长度）")
    void alreadyMaskedFormSurvives() {
        String masked = "ab***yz(len=43)";

        assertThat(LogRedactor.redact("sharedKey=" + masked)).contains(masked);
    }

    @Test
    @DisplayName("正常文本不被改动（脱敏不是「见到 key 字样就乱替」）")
    void normalTextUntouched() {
        assertThat(LogRedactor.redact("检索命中 3 条，耗时 57ms")).isEqualTo("检索命中 3 条，耗时 57ms");
    }

    @Test
    @DisplayName("qlen 只记长度：完整用户输入不进日志（规范 §7）")
    void qlenOnlyRecordsLength() {
        assertThat(LogRedactor.qlen("无人机怎么起飞")).isEqualTo("len=7");
        assertThat(LogRedactor.qlen(null)).isEqualTo("len=0");
    }

    // ---- S-8 PII（手机号/身份证/邮箱，与 Python tests/test_pii.py 同源用例）----

    @Test
    @DisplayName("S-8 手机号被替换为 ***")
    void phoneNumberIsMasked() {
        assertThat(LogRedactor.redact("联系我 13812345678 谢谢")).isEqualTo("联系我 *** 谢谢");
    }

    @Test
    @DisplayName("S-8 身份证（含 X 校验位）被替换为 ***")
    void idCardIsMasked() {
        assertThat(LogRedactor.redact("身份证 11010119900307851X 登记过了"))
                .isEqualTo("身份证 *** 登记过了");
        assertThat(LogRedactor.redact("id=110101199003078515")).isEqualTo("id=***");
    }

    @Test
    @DisplayName("S-8 邮箱被替换为 ***")
    void emailIsMasked() {
        assertThat(LogRedactor.redact("发送到 zhang.san@example.com 即可"))
                .isEqualTo("发送到 *** 即可");
    }

    @Test
    @DisplayName("S-8 顺序契约：手机号邮箱必须整体脱掉，不能剩 ***@139.com 残渣")
    void phoneStyleEmailIsFullyMasked() {
        // 若数字规则先跑，剩 "***@139.com" 里 '*' 不在邮箱字符集里，永远救不回来
        assertThat(LogRedactor.redact("邮箱 13812345678@139.com")).isEqualTo("邮箱 ***");
    }

    @Test
    @DisplayName("S-8 反例：非 PII 文本一字不动（含 11 位非手机号段与超长数字串）")
    void nonPiiTextUntouched() {
        assertThat(LogRedactor.redact("订单号 1234567890123456789012345"))
                .isEqualTo("订单号 1234567890123456789012345");
        assertThat(LogRedactor.redact("编号 12345678901"))
                .isEqualTo("编号 12345678901");
        assertThat(LogRedactor.redact("检索命中 3 条，耗时 57ms"))
                .isEqualTo("检索命中 3 条，耗时 57ms");
    }

    @Test
    @DisplayName("S-8 密钥与 PII 同现：两类都消失")
    void keyAndPiiTogetherBothMasked() {
        String out = LogRedactor.redact("user 13812345678 with X-Agent-Key: abc123def456 done");

        assertThat(out).doesNotContain("13812345678");
        assertThat(out).doesNotContain("abc123def456");
    }

    @Test
    @DisplayName("S-8 hex 感知：16 位 hex traceId 内嵌的 11 位数字段不被打花（真机存量实证）")
    void traceIdInsideLogMessageIsNotCorrupted() {
        // "1db4f13378864346" 内嵌连续 11 位十进制 "13378864346"，纯数字环视会误判成手机号
        assertThat(LogRedactor.redact("trace 1db4f13378864346 failed"))
                .isEqualTo("trace 1db4f13378864346 failed");
    }
}
