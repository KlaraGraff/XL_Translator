;;; 在 AutoCAD（含 AcCoreConsole）里量每段单行文字的真实宽度，配合 cad_text_report.py 用。
;;;
;;; 用法：打开译图后 (load "cad_text_extents.lsp") (xlt-text-extents "/path/out.tsv")
;;; AcCoreConsole：-i 译图.dwg -s 脚本.scr，脚本里写上面两句再 QUIT。
;;;
;;; 每行：句柄、类型、72、存着的起点 x y、对齐点 x y、AutoCAD 量出的宽度、
;;; AutoCAD 按对齐点算出的起点应在的 x y（右对齐 = 对齐点往回一个宽度，居中 = 半个）、文字。
;;; 宽度来自 textbox（按文字自己的字体、字高、宽度比例），不含旋转。

(defun xlt--row (out ed / kind box width h72 p10 p11 ang back start)
  (setq kind (cdr (assoc 0 ed)))
  (setq box (textbox (subst '(0 . "TEXT") (assoc 0 ed) ed)))
  (setq width (if box (- (car (cadr box)) (car (car box))) -1.0))
  (setq h72 (cdr (assoc 72 ed)))
  (setq p10 (cdr (assoc 10 ed)))
  (setq p11 (cdr (assoc 11 ed)))
  (setq ang (cdr (assoc 50 ed)))
  (setq back (cond ((= h72 2) width) ((member h72 '(1 4)) (/ width 2.0)) (t 0.0)))
  (setq start (if (and p11 (member h72 '(1 2 4)))
                (list (- (car p11) (* back (cos ang))) (- (cadr p11) (* back (sin ang))))
                p10))
  (write-line
    (strcat (cdr (assoc 5 ed)) "\t" kind "\t" (itoa h72) "\t"
            (rtos (car p10) 2 6) "\t" (rtos (cadr p10) 2 6) "\t"
            (if p11 (rtos (car p11) 2 6) "") "\t" (if p11 (rtos (cadr p11) 2 6) "") "\t"
            (rtos width 2 6) "\t" (rtos (car start) 2 6) "\t" (rtos (cadr start) 2 6) "\t"
            (cdr (assoc 1 ed)))
    out))

(defun xlt-text-extents (path / out ss i e ed sub)
  (setq out (open path "w"))
  (write-line "handle\ttype\th72\tx10\ty10\tx11\ty11\tacad_width\tacad_x10\tacad_y10\ttext" out)
  (if (setq ss (ssget "X" '((0 . "TEXT,INSERT"))))
    (progn
      (setq i 0)
      (repeat (sslength ss)
        (setq e (ssname ss i) ed (entget e))
        (if (= (cdr (assoc 0 ed)) "TEXT")
          (xlt--row out ed)
          (progn
            (setq sub (entnext e))
            (while (and sub (= (cdr (assoc 0 (setq ed (entget sub)))) "ATTRIB"))
              (xlt--row out ed)
              (setq sub (entnext sub)))))
        (setq i (1+ i)))))
  (close out)
  (princ (strcat "\nxlt-text-extents: " path))
  (princ))
