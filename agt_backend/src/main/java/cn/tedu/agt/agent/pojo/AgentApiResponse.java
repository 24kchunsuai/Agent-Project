package cn.tedu.agt.agent.pojo;

import io.swagger.v3.oas.annotations.media.Schema;
import lombok.Getter;
import lombok.Setter;

import java.util.Map;

/**
 * Python 侧统一响应体的反序列化载体（字段与 Python 的 ApiResponse 一致）。
 *
 * @param <T> 业务数据类型
 */
@Setter
@Getter
@Schema(description = "智能体服务统一响应体")
public class AgentApiResponse<T> {

    @Schema(description = "业务状态码：2000 成功，其余为失败")
    private Integer code;

    @Schema(description = "提示信息")
    private String message;

    @Schema(description = "业务数据")
    private T data;

    public boolean isSuccess() {
        return code != null && code == 2000;
    }

    /**
     * 是否为「确认门」结果（G2 语料治理）：下游要求用户二次确认后才执行，本次并未执行。
     *
     * <p>Python 侧确认门用 {@code 3001 + data.requireConfirm=true} 返回，data 里带着
     * 「该复述哪个文档名 / 命中了哪些相似文档、相似度多少」——这是前端弹确认框的唯一依据。
     * 所以拆包时必须先认这个标记，把它转成 {@code ConfirmRequiredException} 带着 data 出去，
     * 而不是当成普通失败把 data 丢掉。</p>
     */
    public boolean isConfirmRequired() {
        return data instanceof Map<?, ?> map && Boolean.TRUE.equals(map.get("requireConfirm"));
    }
}
