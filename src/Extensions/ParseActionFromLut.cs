using Bonsai;
using System;
using System.Linq;
using System.Reactive.Linq;
using Bonsai.Harp;
using AindBehaviorTelekinesisDataSchema;
using System.Xml.Serialization;
using System.ComponentModel;
using OpenCV.Net;

public class ParseAction : Transform<Timestamped<ActionVector>, Timestamped<ParsedAction>>
{

    public ActionLookUpTableFactory LutSettings { get; set; }

    [XmlIgnore]
    [TypeConverter("Bonsai.Dsp.MatConverter, Bonsai.Dsp")]
    public Mat LookUpTable { get; set; }

    public IObservable<Timestamped<ParsedAction>> Process(IObservable<Timestamped<Tuple<double, double>>> source)
    {
        return Process(source.Select(value => Timestamped.Create(new ActionVector(value.Value), value.Seconds)));
    }


    public override IObservable<Timestamped<ParsedAction>> Process(IObservable<Timestamped<ActionVector>> source)
    {
        Mat lookUpTable;
        ActionLookUpTableFactory lutSettings = LutSettings;
        SubPixelBilinearInterpolator interpolator = new SubPixelBilinearInterpolator();
        if (LookUpTable == null)
        {
            throw new InvalidOperationException("LookUpTable must be specified.");
        }
        lookUpTable = LookUpTable.Clone();
        interpolator = new SubPixelBilinearInterpolator(lutSettings, lookUpTable);
        interpolator.Validate();
        return source.Select(value =>
        {
            var result = interpolator.LookUp(value.Value);
            var parsedAction = new ParsedAction
            {
                Action0 = result.ActionVector.Action0,
                Action1 = result.ActionVector.Action1,
                ProjectedAction = result.ProjectedAction,
                SampledCoordinate0 = result.LookUpIndex.Action0,
                SampledCoordinate1 = result.LookUpIndex.Action1
            };
            return Timestamped.Create(parsedAction, value.Seconds);
        });
    }
}

public class ActionVector
{
    public double Action0 { get; set; }

    public double Action1 { get; set; }


    public ActionVector(double action0, double action1)
    {
        Action0 = action0;
        Action1 = action1;
    }

    public ActionVector(Tuple<double, double> value)
    {
        Action0 = value.Item1;
        Action1 = value.Item2;
    }

    public double this[int index]
    {
        get
        {
            if (index == 0) return Action0;
            if (index == 1) return Action1;
            throw new IndexOutOfRangeException();
        }
    }

    public override string ToString()
    {
        return string.Format("Action0={0}, Action1={1}", Action0, Action1);
    }

}

public class ActionVectorFromLut<T> where T : ActionVector
{
    public T ActionVector { get; set; }

    public double ProjectedAction { get; set; }

    public T LookUpIndex { get; set; }

    public ActionVectorFromLut(T ActionVector, double ProjectedAction, T LookUpIndex)
    {
        this.ActionVector = ActionVector;
        this.ProjectedAction = ProjectedAction;
        this.LookUpIndex = LookUpIndex;
    }
}


public class SubPixelBilinearInterpolator
{
    public SubPixelBilinearInterpolator(ActionLookUpTableFactory settings, Mat lookUpTable)
    {
        Settings = settings;
        LookUpTable = lookUpTable;
    }

    public SubPixelBilinearInterpolator() { }

    public ActionLookUpTableFactory Settings { get; private set; }

    public Mat LookUpTable { get; private set; }

    public void Validate()
    {
        if (Settings == null)
        {
            throw new InvalidOperationException("LUT Settings must be specified.");
        }
        if (LookUpTable.Channels > 1)
        {
            throw new ArgumentException("Input matrix must have a single channel");
        }
        if (LookUpTable.Rows < 2)
        {
            throw new ArgumentException("LUT must have at least 2 rows.");
        }
        if (LookUpTable.Cols < 1)
        {
            throw new ArgumentException("LUT must have at least 1 column.");
        }
        if (Settings.Action0Min >= Settings.Action0Max)
        {
            throw new ArgumentException("Action0: Minimum must be strictly lower than maximum.");
        }
        if (LookUpTable.Cols > 1 && Settings.Action1Min >= Settings.Action1Max)
        {
            throw new ArgumentException("Action1: Minimum must be strictly lower than maximum.");
        }
    }

    public ActionVectorFromLut<ActionVector> LookUp(ActionVector value)
    {
        var h = LookUpTable.Rows;
        var w = LookUpTable.Cols;

        var a0 = Rescale(value.Action0, Settings.Action0Min, Settings.Action0Max, 0, h - 1);
        a0 = ClampValue(a0, 0, h - 1);

        if (w == 1)
        {
            var result = GetSubPixel1D(LookUpTable, a0);
            return new ActionVectorFromLut<ActionVector>(value, result, new ActionVector(a0, double.NaN));
        }
        else
        {
            var a1 = Rescale(value.Action1, Settings.Action1Min, Settings.Action1Max, 0, w - 1);
            a1 = ClampValue(a1, 0, w - 1);
            // LUT matrices are authored as matrix[row = Action1, col = Action0] (see task_gui.py's
            // compute_lut_matrix), so the row/col arguments here must be swapped relative to a0/a1.
            var result = GetSubPixel2D(LookUpTable, a1, a0);
            return new ActionVectorFromLut<ActionVector>(value, result, new ActionVector(a0, a1));
        }
    }

    private static double Rescale(double value, double minFrom, double maxFrom, double minTo, double maxTo)
    {
        return (value - minFrom) / (maxFrom - minFrom) * (maxTo - minTo) + minTo;
    }

    private static double ClampValue(double value, double MinBoundTo, double MaxBoundTo)
    {
        return Math.Min(Math.Max(value, MinBoundTo), MaxBoundTo);
    }

    private static double GetSubPixel1D(Mat src, double y)
    {
        var idx = (int)y;
        var d = y - idx;
        idx = Math.Min(idx, src.Rows - 2);
        var p0 = src[idx, 0];
        var p1 = src[idx + 1, 0];
        return p0.Val0 * (1 - d) + p1.Val0 * d;
    }

    private static double GetSubPixel2D(Mat src, double y, double x)
    {
        var y0 = (int)y;
        var x0 = (int)x;
        var dy = y - y0;
        var dx = x - x0;
        y0 = Math.Min(y0, src.Rows - 2);
        x0 = Math.Min(x0, src.Cols - 2);
        var p00 = src[y0, x0];
        var p01 = src[y0, x0 + 1];
        var p10 = src[y0 + 1, x0];
        var p11 = src[y0 + 1, x0 + 1];
        return p00.Val0 * (1 - dx) * (1 - dy)
             + p01.Val0 * dx * (1 - dy)
             + p10.Val0 * (1 - dx) * dy
             + p11.Val0 * dx * dy;
    }
}
