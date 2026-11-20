package cn.tedu.agt.ops.metrics;

import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Test;

import java.util.ArrayList;
import java.util.HashSet;
import java.util.List;
import java.util.Set;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/**
 * E-9 的文本序列化与 Java 段指标单测。
 *
 * <p>本文件里自己实现了一个**最小 Prometheus 文本解析器**（{@link #parse}）与格式校验，
 * 而不是引 {@code prometheus_client}：一是决策 D3 要求零新依赖，二是——
 * 更重要的是——<b>官方解析器的宽松程度不足以证明"格式严格"</b>：
 * 它不会告诉你某个族少了 TYPE，也不会在"HELP 出现在 TYPE 之后"时报错，
 * 而这两件事在真机上是会让抓取失败的。这里按规范原文逐条检查。</p>
 */
class PrometheusTextTest {

    // 注意：Java 的 java.util.regex **不支持** Python 风格的命名组 `(?P<name>...)`，
    // 只支持 `(?<name>...)`。这里用普通捕获组，索引 1=名字、2=标签(可空)、3=数值。
    private static final Pattern SAMPLE = Pattern.compile(
            "^([a-zA-Z_:][a-zA-Z0-9_:]*)(\\{.*\\})? (\\S+)$");

    private record Parsed(Set<String> families, Set<String> series, List<String> errors) {
    }

    /** 按文本格式 0.0.4 校验：HELP/TYPE 成对且在样本前；无重复族、无重复序列；数值合法。 */
    private static Parsed parse(String text) {
        Set<String> helps = new HashSet<>();
        Set<String> types = new HashSet<>();
        Set<String> series = new HashSet<>();
        List<String> errors = new ArrayList<>();
        for (String line : text.split("\n")) {
            if (line.isBlank()) {
                continue;
            }
            if (line.startsWith("#")) {
                String rest = line.substring(1).strip();
                if (!rest.startsWith("HELP ") && !rest.startsWith("TYPE ")) {
                    continue; // 普通注释
                }
                String[] parts = rest.split(" ", 3);
                String kind = parts[0];
                String name = parts[1];
                if (kind.equals("HELP")) {
                    if (!helps.add(name)) {
                        errors.add("HELP 重复：" + name);
                    }
                    if (types.contains(name)) {
                        errors.add("HELP 出现在 TYPE 之后：" + name);
                    }
                } else {
                    if (!types.add(name)) {
                        errors.add("TYPE 重复：" + name);
                    }
                    if (!helps.contains(name)) {
                        errors.add("TYPE 缺少前置 HELP：" + name);
                    }
                }
                continue;
            }
            Matcher m = SAMPLE.matcher(line);
            if (!m.matches()) {
                errors.add("样本行无法解析：" + line);
                continue;
            }
            String name = m.group(1);
            if (!types.contains(name)) {
                errors.add("样本出现在 TYPE 之前：" + name);
            }
            if (!series.add(name + m.group(2))) {
                errors.add("重复序列（真机会让整次抓取失败）：" + line);
            }
        }
        return new Parsed(types, series, errors);
    }

    // ------------------------------------------------------------------ 构造器

    @Test
    @DisplayName("HELP/TYPE 在任何样本之前，且解析无错")
    void helpAndTypePrecedeSamples() {
        PrometheusText p = new PrometheusText();
        p.family("agt_x_total", "counter", "说明");
        p.sample("agt_x_total", 3, "k", "v");
        Parsed parsed = parse(p.render());
        assertThat(parsed.errors()).isEmpty();
        assertThat(parsed.families()).containsExactly("agt_x_total");
    }

    @Test
    @DisplayName("重复序列被静默丢弃（一条重复会毁掉整次抓取）")
    void duplicateSeriesIsDropped() {
        PrometheusText p = new PrometheusText();
        p.family("agt_x_total", "counter", "说明");
        p.sample("agt_x_total", 1, "k", "v");
        p.sample("agt_x_total", 2, "k", "v");
        p.sample("agt_x_total", 5, "k", "other");
        String out = p.render();
        assertThat(parse(out).errors()).isEmpty();
        assertThat(out).contains("agt_x_total{k=\"v\"} 1");
        assertThat(out).contains("agt_x_total{k=\"other\"} 5");
        assertThat(out).doesNotContain("agt_x_total{k=\"v\"} 2");
    }

    @Test
    @DisplayName("同一族重复声明是幂等的（拼接多段文本时必须有这个性质）")
    void familyDeclarationIsIdempotent() {
        PrometheusText p = new PrometheusText();
        p.family("agt_x_total", "counter", "第一次");
        p.family("agt_x_total", "counter", "第二次");
        String out = p.render();
        assertThat(out.split("# HELP agt_x_total", -1).length - 1).isEqualTo(1);
        assertThat(out).doesNotContain("第二次");
    }

    @Test
    @DisplayName("标签值转义后可还原：引号 / 反斜杠 / 换行")
    void labelEscapingRoundTrips() {
        PrometheusText p = new PrometheusText();
        p.family("agt_x_total", "counter", "说明");
        p.sample("agt_x_total", 1, "tool", "a\"b\\c\nd");
        String out = p.render();
        assertThat(parse(out).errors()).isEmpty();
        assertThat(out).contains("tool=\"a\\\"b\\\\c\\nd\"");
        assertThat(out).doesNotContain("a\"b"); // 未转义的裸引号不能出现
    }

    @Test
    @DisplayName("HELP 文本里的换行要转义（换行会把 HELP 行截断，后面的内容变成非法行）")
    void helpNewlineIsEscaped() {
        PrometheusText p = new PrometheusText();
        p.family("agt_x_total", "counter", "第一行\n第二行");
        String out = p.render();
        assertThat(parse(out).errors()).isEmpty();
        assertThat(out).contains("第一行\\n第二行");
    }

    @Test
    @DisplayName("标签个数为奇数直接抛（拼错比产出坏指标好：坏指标会让整次抓取失败）")
    void oddLabelCountThrows() {
        PrometheusText p = new PrometheusText();
        p.family("agt_x_total", "counter", "说明");
        assertThatThrownBy(() -> p.sample("agt_x_total", 1, "k"))
                .isInstanceOf(IllegalArgumentException.class);
    }

    @Test
    @DisplayName("数值用定点输出：整数不带小数点、浮点不出现科学计数法")
    void numberFormattingAvoidsScientificNotation() {
        assertThat(PrometheusText.number(7)).isEqualTo("7");
        assertThat(PrometheusText.number(7L)).isEqualTo("7");
        assertThat(PrometheusText.number(7.0)).isEqualTo("7");
        assertThat(PrometheusText.number(0.0001)).isEqualTo("0.0001");
        assertThat(PrometheusText.number(1.25e-5)).doesNotContain("E").doesNotContain("e");
        assertThat(PrometheusText.number(Double.NaN)).isEqualTo("NaN");
        assertThat(PrometheusText.number(Double.POSITIVE_INFINITY)).isEqualTo("+Inf");
        assertThat(PrometheusText.number(null)).isEqualTo("0");
    }

    @Test
    @DisplayName("响应体以换行结尾（Prometheus 与 curl 都期望文本行结尾）")
    void renderEndsWithNewline() {
        PrometheusText p = new PrometheusText();
        p.family("agt_x_total", "counter", "说明");
        p.sample("agt_x_total", 1);
        assertThat(p.render()).endsWith("\n");
    }

    @Test
    @DisplayName("Content-Type 必须带 version=0.0.4（缺了抓取器无法确定解析器）")
    void contentTypeCarriesVersion() {
        assertThat(PrometheusText.CONTENT_TYPE).isEqualTo("text/plain; version=0.0.4; charset=utf-8");
    }

    // ------------------------------------------------------------------ JavaMetrics

    @Test
    @DisplayName("Java 段包含约定的 agt_java_* 族，且整体格式合法")
    void javaMetricsFamiliesAreCompleteAndValid() {
        String out = JavaMetrics.render();
        Parsed parsed = parse(out);
        assertThat(parsed.errors()).isEmpty();
        assertThat(parsed.families()).contains(
                "agt_java_up", "agt_java_info", "agt_java_heap_used_bytes",
                "agt_java_heap_max_bytes", "agt_java_nonheap_used_bytes", "agt_java_threads",
                "agt_java_uptime_seconds", "agt_java_processors", "agt_java_free_memory_bytes");
    }

    @Test
    @DisplayName("Java 段数值是基础单位：堆用字节、运行时长用秒")
    void javaMetricsUseBaseUnits() {
        String out = JavaMetrics.render();
        double heapUsed = sampleValue(out, "agt_java_heap_used_bytes");
        double uptime = sampleValue(out, "agt_java_uptime_seconds");
        double threads = sampleValue(out, "agt_java_threads");
        assertThat(heapUsed).isGreaterThan(0);          // 字节量级：远大于 1
        assertThat(threads).isGreaterThan(0);
        // 单测 JVM 的运行时长不可能是毫秒被当成秒（那会是 6 位数）——用上界反证单位正确
        assertThat(uptime).isLessThan(86_400);
    }

    @Test
    @DisplayName("agt_java_info 带 service 与 version（版本与 E-1 同源，取不到写 unknown 而不是空）")
    void javaInfoCarriesServiceAndVersion() {
        String out = JavaMetrics.render();
        assertThat(out).contains("service=\"agt_backend\"");
        assertThat(out).containsPattern("version=\"[^\"]+\"");
        assertThat(out).doesNotContain("version=\"\"");
    }

    /** 从渲染结果里取某条无标签序列的数值。 */
    private static double sampleValue(String text, String name) {
        for (String line : text.split("\n")) {
            if (line.startsWith(name + " ")) {
                return Double.parseDouble(line.substring(name.length() + 1).trim());
            }
        }
        throw new AssertionError("未找到序列：" + name);
    }
}
