package cn.tedu.agt.ops.metrics;

import java.math.BigDecimal;
import java.util.ArrayList;
import java.util.HashSet;
import java.util.List;
import java.util.Set;

/**
 * 极小的 Prometheus 文本格式构造器（企业级落地规约 <b>E-9</b>，判据 C3）。
 *
 * <h3>为什么要自己写</h3>
 * 决策 D3 定为"可观测轻量自建、不引外部组件"，Java 侧如果为一个几行的 layout 引入
 * Micrometer + Prometheus registry，等于给整个后端拉一套指标框架（连同它的
 * {@code /actuator} 端点与自动配置），**为了几十行文本换来一个需要长期跟版本的安全面**。
 * 本类做的事只有三件：转义、去重、拼行。
 *
 * <h3>它保证的正是"Prometheus 不报错"的三个硬约束</h3>
 * <ol>
 *   <li><b>{@code HELP}/{@code TYPE} 成对且在任何样本之前</b>——顺序错了抓取直接失败；</li>
 *   <li><b>同名 + 同标签不允许出现两条</b>——真机报
 *       {@code duplicate sample for timestamp}，<b>会让整次抓取失败</b>，
 *       一条重复就毁掉全部指标，所以这里用 Set 静默丢弃重复（而非拼出来再说）；</li>
 *   <li><b>标签值必须转义</b> {@code \} {@code "} 换行——工具名/任务名将来带这些字符时，
 *       不转义会让解析器在中间断掉，症状是"整份指标看不到了"。</li>
 * </ol>
 *
 * <p>数值一律以<b>十进制定点</b>输出：{@code 1.0E-4} 这种科学计数法 Prometheus 虽然也认，
 * 但人读痛苦、抄进文档容易错。</p>
 */
public final class PrometheusText {

    /** Prometheus 文本格式的 Content-Type。<b>版本号不能省</b>：抓取器按它选解析器。 */
    public static final String CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8";

    private final List<String> lines = new ArrayList<>();
    private final Set<String> declared = new HashSet<>();
    private final Set<String> emitted = new HashSet<>();

    /** 声明一个指标族。同一族重复声明无副作用（幂等）。 */
    public void family(String name, String type, String help) {
        if (!declared.add(name)) {
            return;
        }
        lines.add("# HELP " + name + " " + escapeHelp(help));
        lines.add("# TYPE " + name + " " + type);
    }

    /** 写一条无标签序列。 */
    public void sample(String name, Number value) {
        write(name, value, "");
    }

    /**
     * 写一条带标签序列。
     *
     * @param labels 形如 {@code {"task", "reconcile", "status", "DONE"}} 的键值对平铺数组
     */
    public void sample(String name, Number value, String... labels) {
        if (labels.length % 2 != 0) {
            throw new IllegalArgumentException("标签必须成对给出：" + name);
        }
        StringBuilder sb = new StringBuilder();
        if (labels.length > 0) {
            sb.append('{');
            for (int i = 0; i < labels.length; i += 2) {
                if (i > 0) {
                    sb.append(',');
                }
                sb.append(labels[i]).append("=\"").append(escapeLabel(labels[i + 1])).append('"');
            }
            sb.append('}');
        }
        write(name, value, sb.toString());
    }

    private void write(String name, Number value, String labelText) {
        if (!emitted.add(name + labelText)) {
            return; // 重复序列 = 抓取失败，宁可丢一条也不让整份指标作废
        }
        lines.add(name + labelText + " " + number(value));
    }

    /** 写注释行。Prometheus 会把非 HELP/TYPE 的 {@code #} 行当注释忽略，适合写"某段为何缺失"。 */
    public void comment(String text) {
        for (String part : text.split("\n")) {
            lines.add(("# " + part).stripTrailing());
        }
    }

    public String render() {
        return String.join("\n", lines) + "\n";
    }

    // ------------------------------------------------------------------ 转义与数值

    /** 标签值转义：文本格式里只有三个必须处理的字符。 */
    public static String escapeLabel(String raw) {
        if (raw == null) {
            return "";
        }
        return raw.replace("\\", "\\\\").replace("\"", "\\\"").replace("\n", "\\n");
    }

    /** HELP 文本转义：只处理 {@code \} 与换行（引号在 HELP 里是合法的）。 */
    public static String escapeHelp(String raw) {
        return raw == null ? "" : raw.replace("\\", "\\\\").replace("\n", "\\n");
    }

    /** 数值序列化：整数不带小数点，浮点用定点且去掉尾随零。 */
    public static String number(Number value) {
        if (value == null) {
            return "0";
        }
        if (value instanceof Integer || value instanceof Long || value instanceof Short
                || value instanceof Byte) {
            return value.toString();
        }
        double d = value.doubleValue();
        if (Double.isNaN(d)) {
            return "NaN";
        }
        if (Double.isInfinite(d)) {
            return d > 0 ? "+Inf" : "-Inf";
        }
        if (d == Math.rint(d) && Math.abs(d) < 1e15) {
            return Long.toString((long) d);
        }
        return BigDecimal.valueOf(d).stripTrailingZeros().toPlainString();
    }
}
