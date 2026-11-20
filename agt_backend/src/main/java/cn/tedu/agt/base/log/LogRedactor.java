package cn.tedu.agt.base.log;

import java.util.LinkedHashSet;
import java.util.Set;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

/**
 * 日志脱敏（企业级落地规约 <strong>E-6</strong>，与 <strong>S-8</strong> 同源）。
 *
 * <p>这是"日志不得出现密钥/token/完整用户输入"的<strong>唯一实现处</strong>，
 * 由 {@link JsonLogLayout} 在写出前统一调用。刻意不靠"写日志时记得别写"——
 * 靠自觉迟早会漏一次，而漏一次密钥就永久留在磁盘上。</p>
 *
 * <h3>两层防线</h3>
 * <ol>
 *   <li><strong>字面量</strong>（{@link #registerSecret}）：把真实密钥值本身登记进来，
 *       命中即替换。与措辞无关，所以"换个说法"也漏不出去——这是主力。</li>
 *   <li><strong>正则</strong>：兜住"值还没配到 / 值来自请求头"的场景（如把用户传来的
 *       {@code Authorization} 头打进日志）。</li>
 * </ol>
 *
 * <p>与 Python 侧 {@code app/core/logging_setup.py} 的 {@code redact()} 保持同一套规则；
 * 改这里要同步改那边（字段与规则清单见 {@code docs/日志规范.md} §7）。</p>
 */
public final class LogRedactor {

    /** 短于此长度的"密钥"不当字面量替换，否则正常文本（如 {@code true}）会被打成 ***。 */
    private static final int MIN_SECRET_LEN = 8;

    private static final Set<String> SECRETS = new LinkedHashSet<>();

    /**
     * 第二层规则。<strong>顺序有意义</strong>：{@code Bearer} 那条必须排在
     * {@code Authorization} 那条<strong>前面</strong>——否则
     * {@code Authorization: Bearer eyJ...} 会先被 authorization 规则把值 "Bearer"
     * 替掉，真正的 token 反而留在日志里（Python 侧首次跑单测就抓到了这个缺陷，
     * 见 {@code tests/test_logging.py::test_bearer_token_is_masked}）。
     */
    private static final Pattern[] PATTERNS = {
            // Bearer <token>：值必须"像 token"（6 位以上），避免把 "Bearer 已过期" 打花
            Pattern.compile("(?i)\\bbearer\\s+([A-Za-z0-9._-]{6,})"),
            // X-Agent-Key: v / AGENT_SHARED_KEY=v / x-agent-key v
            // 值部分要求 6 位以上 [A-Za-z0-9._-]：这样 AgentAuth.mask() 产出的
            // "ab***yz(len=43)" 不会被二次破坏（'*' 不在字符集里，凑不满 6 位）。
            Pattern.compile("(?i)(x-agent-key|agent[_-]?shared[_-]?key)\\s*[:=]?\\s*([A-Za-z0-9._-]{6,})"),
            // Authorization: <value>（含 "Bearer" 已被上一条吃掉后的残形）
            Pattern.compile("(?i)(authorization|proxy-authorization)\\s*[:=]\\s*(?:Bearer\\s+)?(\\S+)"),
            Pattern.compile("(?i)\\b(x-token|agt[_-]?token|access[_-]?token|refresh[_-]?token|token)\\s*[:=]\\s*(\\S+)"),
            // JSON 形态：{"apiKey":"sk-xxx"} / {"password":"xxx"}
            Pattern.compile("(?i)\"(api[_-]?key|secret|password|passwd|pwd|shared[_-]?key)\"\\s*:\\s*\"[^\"]*\""),
            // 常见第三方密钥形态
            Pattern.compile("\\b(?:sk|pk|rk)-[A-Za-z0-9_-]{8,}"),
            // ---- S-8 PII（手机号/身份证/邮箱，与 Python app/core/pii.py 同规则）----
            // 邮箱必须最先：13812345678@139.com 若先被手机号规则吃掉数字，剩下的
            // "***@139.com" 里 '*' 不在邮箱字符集里，邮箱规则就再也吃不掉它。
            // 邮箱：常见形态从宽匹配（日志场景"漏脱"比"误脱"严重）
            Pattern.compile("[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\\.[A-Za-z]{2,}"),
            // 身份证在手机号**前面**（18 位内部含 11 位手机号形态子串；两侧环视
            // 保证互不踩踏，顺序是留档的契约）。
            // 两侧环视用 hex 感知（(?<![0-9a-fA-F]) 而非 (?<!\\d)）：真机存量扫描实证，
            // 16 位 hex traceId "1db4f13378864346" 内嵌 11 位十进制段 "13378864346"，
            // 纯数字环视会把 traceId 打花。
            // 身份证不做校验位验证：宁可将 18 位订单号误脱，也不放过真身份证。
            Pattern.compile("(?<![0-9a-fA-F])\\d{17}[\\dXx](?![0-9a-fA-F])"),
            // 手机号：1[3-9] 开头 11 位
            Pattern.compile("(?<![0-9a-fA-F])1[3-9]\\d{9}(?![0-9a-fA-F])"),
    };

    private static final String[] REPLACEMENTS = {
            "Bearer ***",
            "$1=***",
            "$1 ***",
            "$1=***",
            "\"$1\":\"***\"",
            "***",
            "***",  // S-8 邮箱
            "***",  // S-8 身份证
            "***",  // S-8 手机号
    };

    static {
        // 主力字面量：进程环境里能拿到的真实密钥。
        // 放在静态块里（而不是等 Spring 装配）是因为 logback 的 layout 由 logback
        // 自己实例化，早于 Spring 容器；静态块能保证"第一条日志写出前就已登记"。
        registerSecret(System.getenv("AGENT_SHARED_KEY"));
        // E-10 密钥轮换：轮换期上一把密钥在过渡窗口内**仍是有效凭据**
        // （Python 侧同时接受两把），漏掉它就等于让一把还能开门的钥匙
        // 以明文出现在日志里。Java 侧只"发送"密钥、不校验，故不需要
        // 双密钥发送逻辑；但**脱敏名单必须跟着一起轮换**。
        registerSecret(System.getenv("AGENT_SHARED_KEY_PREV"));
    }

    private LogRedactor() {
    }

    /** 登记一个真实密钥值：此后它出现在任何日志里都会被替换成 {@code ***}。 */
    public static void registerSecret(String value) {
        if (value != null && value.trim().length() >= MIN_SECRET_LEN) {
            SECRETS.add(value.trim());
        }
    }

    /**
     * 抹掉文本里的密钥 / token。只做减法，<strong>绝不抛异常</strong>：
     * 日志出口失败会连带影响正常业务，代价远大于"少脱敏一点"。
     */
    public static String redact(String text) {
        if (text == null || text.isEmpty()) {
            return "";
        }
        try {
            for (String secret : SECRETS) {
                if (text.contains(secret)) {
                    text = text.replace(secret, "***");
                }
            }
            for (int i = 0; i < PATTERNS.length; i++) {
                Matcher matcher = PATTERNS[i].matcher(text);
                if (matcher.find()) {
                    text = matcher.replaceAll(REPLACEMENTS[i]);
                }
            }
        } catch (RuntimeException ignored) {
            // 脱敏失败宁可少脱，也不能让日志写不出去
        }
        return text;
    }

    /**
     * 用户输入的"长度"描述（{@code len=37}）。
     *
     * <p>日志规范 §7 规定"完整用户输入不得进日志，只记长度"——这个方法就是把那条
     * 约定变成"一行可调用的东西"，比在注释里叮嘱可靠。</p>
     *
     * <p>要看内容请用同一个 traceId 查产品数据：
     * {@code GET /v1/agent/metrics/traces/{traceId}}。</p>
     */
    public static String qlen(String text) {
        return "len=" + (text == null ? 0 : text.length());
    }
}
