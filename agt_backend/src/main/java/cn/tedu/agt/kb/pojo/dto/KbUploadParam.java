package cn.tedu.agt.kb.pojo.dto;

import io.swagger.v3.oas.annotations.media.Schema;
import jakarta.validation.constraints.NotBlank;
import jakarta.validation.constraints.Size;
import lombok.Data;

/**
 * 知识库文档上传参数。
 *
 * <p>正文走 JSON 字符串而非 multipart：当前只支持 md/txt 文本格式，
 * 前端用 FileReader 读出文本即可，省掉文件流的中间转发与临时文件管理。</p>
 *
 * <p><strong>owner 不在此列</strong>——文档归属一律由服务端从登录态注入，
 * 前端传什么都不作数，避免越权把自己上传的文档记到别人名下。</p>
 */
@Data
@Schema(description = "知识库文档上传参数")
public class KbUploadParam {

    @NotBlank(message = "文件名不能为空")
    @Size(max = 255, message = "文件名过长")
    @Schema(description = "文件名（含扩展名，支持 .md/.markdown/.txt）", example = "无人机飞行管理规定.md")
    private String fileName;

    @NotBlank(message = "文档内容不能为空")
    @Schema(description = "文档正文（UTF-8 文本）")
    private String content;

    @Size(max = 64, message = "分类名过长")
    @Schema(description = "分类，可空", example = "regulation")
    private String category;

    @Schema(description = "可见性：public（所有人可检索）/ private（仅自己可检索）", example = "public")
    private String visibility = "public";

    // ---- 以下字段由服务端注入，前端传入无效 ----

    @Schema(description = "文档归属用户（服务端注入）", accessMode = Schema.AccessMode.READ_ONLY)
    private String owner;
}
