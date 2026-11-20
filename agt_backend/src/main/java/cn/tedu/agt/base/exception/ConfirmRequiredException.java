package cn.tedu.agt.base.exception;

import cn.tedu.agt.base.response.StatusCode;
import lombok.Getter;

/**
 * 确认门中断（G2 语料治理）：下游判定「该操作需要用户二次确认后才执行」，本次<b>并未执行</b>。
 *
 * <p>为什么不复用普通 {@link ServiceException}：确认门返回的 <b>data 是给前端的确认依据</b>——
 * 彻底删除要告诉用户「该复述哪个文档名」，恢复要告诉用户「命中了哪些相似文档、相似度多少」。
 * 而全局处理器只保留 message（data 固定为 null），一旦按普通错误抛出，前端就只剩一句提示、
 * 只能盲点确认。所以这里单独携带 {@code detail} 原样透传。</p>
 *
 * <p>状态码固定 3001：语义上确实是「缺少确认参数」，且与 Python 侧 {@code _kb_fail} 的
 * 返回口径（3001 + data.requireConfirm）完全一致——前端只需认一套编码与一套解包逻辑。</p>
 */
@Getter
public class ConfirmRequiredException extends ServiceException {

    /** 结构化确认信息，原样来自 Python（requireConfirm / gate / expectedDocName / hits / maxSimilarity ...） */
    private final Object detail;

    public ConfirmRequiredException(String message, Object detail) {
        super(StatusCode.VALIDATE_ERROR, message);
        this.detail = detail;
    }
}
